'use client';

/**
 * RazorpayCheckout - the real-gateway half of the customer recovery page.
 *
 * Rendered only when the backend reports `gateway_mode === "razorpay_test"`,
 * which means Razorpay Test Mode credentials are configured on the server. It
 * loads Razorpay's hosted Checkout script, opens the modal against an order the
 * *server* created, and hands the result back for verification.
 *
 * ---------------------------------------------------------------------------
 * The security property this file exists to preserve
 * ---------------------------------------------------------------------------
 * Razorpay's `handler` callback fires in the customer's browser. It is a CLAIM
 * that a payment succeeded, not proof of one - it is script running on a page
 * the customer controls, and anyone can call it with invented arguments from a
 * console. Nothing in this component marks anything as recovered.
 *
 * What actually moves a case to `recovered` is `POST /api/recovery/cases/{id}/verify`,
 * which recomputes `HMAC-SHA256(secret, "{order_id}|{payment_id}")` server-side
 * and compares it against the signature Razorpay returned. The secret never
 * leaves the backend, so a forged callback cannot produce a signature that
 * survives that check. This component's only job is to relay three opaque
 * strings to that endpoint and render whatever the server says came back.
 *
 * The publishable `key_id` in the session is a different thing and is safe here:
 * it identifies the merchant account to Razorpay and is public by design.
 *
 * ---------------------------------------------------------------------------
 * Script loading
 * ---------------------------------------------------------------------------
 * Checkout.js is injected on mount rather than declared in the document head:
 * it is needed by exactly one screen and only in one of two gateway modes, and
 * a payments SDK on every page of a merchant console is a third-party script on
 * every page of a merchant console.
 *
 * Injection is guarded three ways - an existing `window.Razorpay`, an existing
 * tag with the same `src`, and a timeout - because React StrictMode mounts every
 * component twice in development, and because a script that is blocked (an ad
 * blocker, an offline laptop, a corporate proxy) frequently fires neither
 * `load` nor `error`.
 */

import * as React from 'react';
import { ExternalLink, ShieldCheck, WifiOff } from 'lucide-react';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { api } from '@/lib/api';
import { formatRupees } from '@/lib/format';
import type { CheckoutSession, RecoveryCase } from '@/lib/types';
import { errorMessage } from '@/lib/utils';

const CHECKOUT_SCRIPT_SRC = 'https://checkout.razorpay.com/v1/checkout.js';

/**
 * How long to wait for the SDK before declaring it unavailable.
 *
 * A blocked request often hangs rather than erroring, and a customer staring at
 * a permanently disabled "Pay" button has no way to tell a slow network from a
 * broken one. Twelve seconds is past any plausible cold load and short enough
 * that the fallback message arrives while they are still paying attention.
 */
const SCRIPT_TIMEOUT_MS = 12_000;

/* -------------------------------------------------------------------------- */
/* The Razorpay global                                                         */
/* -------------------------------------------------------------------------- */

/** Prefilled customer identity. Razorpay treats every field as optional. */
interface RazorpayPrefill {
  name?: string;
  email?: string;
  contact?: string;
}

/** Accent colour for the hosted modal. Hex string, Razorpay's own format. */
interface RazorpayTheme {
  color?: string;
}

/**
 * What Razorpay hands to `handler` on a successful payment.
 *
 * Three opaque strings. They are relayed to the server unchanged and are never
 * parsed, trusted or acted on here - see the security note at the top.
 */
export interface RazorpayHandlerResponse {
  razorpay_payment_id: string;
  razorpay_order_id: string;
  razorpay_signature: string;
}

interface RazorpayModalOptions {
  /** Fired when the customer closes the modal without paying. */
  ondismiss?: () => void;
  /** Whether Escape closes the modal. */
  escape?: boolean;
}

/**
 * The subset of Razorpay Checkout options this integration uses.
 *
 * Declared as a real interface rather than typed loosely, so that a typo in an
 * option name is a compile error instead of a silently ignored key that makes
 * the modal open with the wrong amount.
 */
export interface RazorpayOptions {
  /** Publishable key id (`rzp_test_...`). Public by design. */
  key: string;
  /**
   * Amount in the smallest currency unit - paise. This is passed straight
   * through from `session.amount_paise`; the server and the gateway agree on
   * paise, so there is no conversion to get wrong here.
   */
  amount: number;
  currency: string;
  /** Merchant name shown as the modal's heading. */
  name: string;
  description: string;
  /** The order created server-side. Checkout will not open without it. */
  order_id: string;
  prefill: RazorpayPrefill;
  theme: RazorpayTheme;
  handler: (response: RazorpayHandlerResponse) => void;
  modal: RazorpayModalOptions;
}

declare global {
  interface Window {
    Razorpay?: new (options: RazorpayOptions) => { open: () => void };
  }
}

/* -------------------------------------------------------------------------- */
/* Component                                                                   */
/* -------------------------------------------------------------------------- */

type ScriptStatus = 'loading' | 'ready' | 'unavailable';

export interface RazorpayCheckoutProps {
  /** The live session: order id, amount, publishable key, customer identity. */
  session: CheckoutSession;
  /** Shown as the modal heading. The wire contract carries no merchant name. */
  merchantName: string;
  /**
   * Called with the authoritative case returned by the server after it has
   * verified the signature. The parent renders the confirmation from this, not
   * from anything the browser callback said.
   */
  onSettled: (updated: RecoveryCase) => void;
}

export function RazorpayCheckout({ session, merchantName, onSettled }: RazorpayCheckoutProps) {
  const [scriptStatus, setScriptStatus] = React.useState<ScriptStatus>('loading');
  const [verifying, setVerifying] = React.useState(false);
  const [failure, setFailure] = React.useState<string | null>(null);

  React.useEffect(() => {
    // A cancellation flag rather than a mounted ref: the load event, the error
    // event and the timeout can all resolve after this component has gone, and
    // each of them would otherwise set state on an unmounted tree.
    let cancelled = false;

    if (window.Razorpay !== undefined) {
      setScriptStatus('ready');
      return;
    }

    const handleLoad = (): void => {
      if (cancelled) return;
      // `load` fired, but the global is what actually matters: a proxy that
      // answers with an HTML error page still fires `load`.
      setScriptStatus(window.Razorpay !== undefined ? 'ready' : 'unavailable');
    };

    const handleError = (): void => {
      if (!cancelled) setScriptStatus('unavailable');
    };

    // Guard against double-injection. StrictMode mounts this twice in
    // development, and two tags for the same SDK means two downloads racing to
    // assign the same global.
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${CHECKOUT_SCRIPT_SRC}"]`,
    );
    const script = existing ?? document.createElement('script');

    script.addEventListener('load', handleLoad);
    script.addEventListener('error', handleError);

    if (existing === null) {
      script.src = CHECKOUT_SCRIPT_SRC;
      script.async = true;
      document.head.appendChild(script);
    }

    // Covers the case neither event does: a blocked or silently dropped request,
    // and a tag left behind by an earlier mount whose `load` already fired
    // before these listeners were attached.
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      setScriptStatus(window.Razorpay !== undefined ? 'ready' : 'unavailable');
    }, SCRIPT_TIMEOUT_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      script.removeEventListener('load', handleLoad);
      script.removeEventListener('error', handleError);
      /*
       * The <script> element itself is deliberately left in the document.
       * Removing it would not unload an SDK that has already run and assigned
       * `window.Razorpay`; it would only guarantee a second download on the next
       * mount - which, under StrictMode, is immediately. The listeners are ours
       * and are removed; the script is the page's and stays.
       */
    };
  }, []);

  /**
   * Relays Razorpay's callback to the server for verification.
   *
   * Note what this does NOT do: it does not set any local "paid" state, does not
   * congratulate the customer, and does not tell the parent anything until the
   * server has answered. Every visible claim of success on this screen is
   * downstream of the HMAC check.
   */
  const verify = React.useCallback(
    async (response: RazorpayHandlerResponse): Promise<void> => {
      setVerifying(true);
      setFailure(null);
      try {
        const updated = await api.verifyPayment(session.case_id, {
          razorpay_order_id: response.razorpay_order_id,
          razorpay_payment_id: response.razorpay_payment_id,
          razorpay_signature: response.razorpay_signature,
        });
        onSettled(updated);
      } catch (err) {
        // A payment that Razorpay accepted but we could not verify is the one
        // case where the customer must not be told "try again" - their money may
        // well have moved. Say what happened and hand it to a human.
        setFailure(
          `${errorMessage(err)} If money has left your account, contact ${merchantName} with reference ${response.razorpay_payment_id} - do not pay again.`,
        );
      } finally {
        setVerifying(false);
      }
    },
    [merchantName, onSettled, session.case_id],
  );

  const keyId = session.razorpay_key_id;

  const openCheckout = (): void => {
    const Razorpay = window.Razorpay;
    if (Razorpay === undefined) {
      setScriptStatus('unavailable');
      return;
    }
    if (keyId === null) return; // Unreachable: the button is not rendered without a key.

    setFailure(null);

    const options: RazorpayOptions = {
      key: keyId,
      amount: session.amount_paise,
      currency: session.currency,
      name: merchantName,
      description: session.description,
      order_id: session.order_id,
      prefill: {
        name: session.customer_name,
        email: session.customer_email,
        contact: session.customer_phone,
      },
      // slate-900: the console's primary ink, so the hosted modal does not
      // arrive in a colour the rest of the product never uses.
      theme: { color: '#0f172a' },
      handler: (response) => {
        // `handler` is not async-aware, so the promise is explicitly discarded;
        // every outcome of `verify` is already handled inside it.
        void verify(response);
      },
      modal: {
        // Dismissal is a normal outcome, not an error. The case stays in
        // `awaiting_payment`, the link stays live, and the page's poll keeps
        // watching - so there is nothing to report and nothing to reset.
        ondismiss: () => undefined,
        escape: true,
      },
    };

    new Razorpay(options).open();
  };

  /*
   * A configuration fault, not a network one: the server says it is in Razorpay
   * mode but sent no publishable key, so Checkout cannot be opened at all. It is
   * called out separately from the script failure because the fix is completely
   * different - one is the operator's `.env`, the other is the customer's
   * network.
   */
  if (keyId === null) {
    return (
      <Alert variant="danger">
        <AlertTitle>This payment page is not configured</AlertTitle>
        <AlertDescription>
          The gateway is running in Razorpay test mode but no publishable key reached this page, so
          Checkout cannot be opened. Contact {merchantName}; nothing has been charged.
        </AlertDescription>
      </Alert>
    );
  }

  if (scriptStatus === 'unavailable') {
    return (
      <Alert variant="warning" icon={<WifiOff className="h-4 w-4" aria-hidden="true" />}>
        <AlertTitle>Razorpay Checkout could not be loaded</AlertTitle>
        <AlertDescription className="space-y-2">
          <p>
            The hosted checkout script at <span className="font-mono text-xs">checkout.razorpay.com</span>{' '}
            did not load. That is usually an offline machine, an ad blocker, or a network that blocks
            third-party scripts. Nothing has been charged.
          </p>
          <p>
            Reload the page once you are back online. To demonstrate the full recovery flow with no
            network at all, restart the backend without Razorpay credentials - it falls back to the
            simulated gateway, which needs nothing external.
          </p>
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-3">
      {failure !== null ? (
        <Alert variant="danger">
          <AlertTitle>We could not confirm that payment</AlertTitle>
          <AlertDescription>{failure}</AlertDescription>
        </Alert>
      ) : null}

      <Button
        variant="success"
        size="lg"
        className="w-full"
        // `loading` covers both waits with one disabled state: the SDK arriving
        // and the server verifying. A customer who double-clicks during either
        // one would otherwise open two modals against the same order.
        loading={scriptStatus === 'loading' || verifying}
        loadingText={verifying ? 'Confirming your payment…' : 'Preparing secure checkout…'}
        trailingIcon={<ExternalLink className="h-4 w-4" aria-hidden="true" />}
        onClick={openCheckout}
      >
        Pay {formatRupees(session.amount_paise)}
      </Button>

      <p className="flex items-center justify-center gap-1.5 text-center text-xs text-slate-500 dark:text-slate-400">
        <ShieldCheck className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        Payment is completed in Razorpay&apos;s own secure window and confirmed on our server before
        anything is marked as paid.
      </p>
    </div>
  );
}
