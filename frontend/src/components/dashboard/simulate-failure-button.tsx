'use client';

/**
 * SimulateFailureButton - manufactures a realistic failed payment on demand.
 *
 * Why a demo control ships in the product UI at all: a reviewer cannot make a
 * real card decline to order. Razorpay's test mode will fail a payment for you,
 * but only on its own terms and only after a checkout round trip, and this app
 * is meant to run with no Razorpay credentials at all. The alternative - a demo
 * seeded with nothing but pre-baked rows - leaves the reviewer able to watch the
 * agent handle inputs the author chose, and unable to test it on one they chose.
 * That is the difference between a demonstration and a claim.
 *
 * So the control is honest rather than hidden: it is labelled as a simulation,
 * it lives beside the mode banner that says the gateway is simulated, and the
 * payment it creates travels the same ingestion path as one that arrived from a
 * webhook. Nothing downstream knows the difference, which is the point - the
 * classifier, the propensity model and all thirteen guardrails run on it for
 * real.
 *
 * The amount presets are chosen to sit either side of the policy thresholds in
 * `backend/app/config.py`, so a reviewer can deliberately trip a guardrail
 * instead of hoping to stumble into one. That is the fastest way to see that
 * the limits are real.
 */

import { useEffect, useState } from 'react';
import { CirclePlus } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogFooter } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import {
  FAILURE_CATEGORY_LABEL,
  PAYMENT_METHOD_LABEL,
  type FailureScenario,
  type PaymentMethod,
  type SimulateFailureRequest,
} from "@/lib/types";
import { cn, errorMessage } from "@/lib/utils";

/** Sentinel for "let the server pick", kept out of the enum's value space. */
const ANY = "any";


/**
 * Amounts are declared as exact integer paise, never as rupees multiplied in
 * the component. Money is integer paise everywhere upstream of the API edge,
 * and a form that computes `rupees * 100` is one rounding rule away from
 * disagreeing with the ledger. The labels are written out by hand for the same
 * reason - they are captions, not conversions.
 */
const AMOUNT_PRESETS: readonly { paise: number | null; label: string; hint?: string }[] = [
  { paise: null, label: "From the scenario", hint: "whatever the catalogue specifies" },
  { paise: 49_900, label: "₹499" },
  { paise: 249_900, label: "₹2,499" },
  { paise: 999_900, label: "₹9,999", hint: "just under the high-value review threshold" },
  { paise: 2_499_900, label: "₹24,999", hint: "above it - forces a human review" },
  { paise: 6_499_900, label: "₹64,999", hint: "above the absolute ceiling - will be denied" },
];

/**
 * `unknown` is omitted deliberately: it is the classifier's "I could not tell"
 * value, not an instrument a customer can pay with, so offering it here would
 * ask the gateway to fail on something that cannot be attempted.
 */
const SELECTABLE_METHODS: readonly PaymentMethod[] = ["card", "upi", "netbanking", "wallet", "emi"];

const FIELD_CLASSES = cn(
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  "disabled:cursor-not-allowed disabled:opacity-50",
);

export function SimulateFailureButton({
  onSimulated,
  className,
}: {
  /** Called after the payment is created, so the dashboard can re-read itself. */
  onSimulated: () => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [scenario, setScenario] = useState<string>(ANY);
  // The catalogue is fetched, never hard-coded. A previous version listed the
  // keys here and six of its eight named scenarios did not exist server-side, so
  // the picker's most important option -- the fraud case the guardrails must
  // refuse -- answered 404. The server owns the list; the client asks for it.
  //
  // Loaded once when the dialog first opens rather than on mount: the dashboard
  // renders this button on every visit and most visits never open it.
  const [scenarios, setScenarios] = useState<readonly FailureScenario[]>([]);
  const [scenariosError, setScenariosError] = useState<string | null>(null);
  const [amountPaise, setAmountPaise] = useState<number | null>(null);
  const [method, setMethod] = useState<PaymentMethod | typeof ANY>(ANY);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || scenarios.length > 0) return;
    let cancelled = false;
    api
      .listFailureScenarios()
      .then((rows) => {
        if (!cancelled) setScenarios(rows);
      })
      .catch((caught) => {
        // Not fatal: "Random" always works, because the server draws from
        // whatever the catalogue actually holds. Say so rather than presenting
        // an empty dropdown that looks broken.
        if (!cancelled) setScenariosError(errorMessage(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [open, scenarios.length]);

  function close(): void {
    setOpen(false);
    // The error is cleared but the selections are kept: a reviewer who hit a
    // problem and reopens the dialog almost always wants to retry the same
    // combination, not to re-pick it from scratch.
    setError(null);
  }

  async function submit(): Promise<void> {
    setSubmitting(true);
    setError(null);

    // Built by assignment rather than with a spread so that "leave it to the
    // server" is expressed by the key being absent, which is exactly what the
    // optional fields on `SimulateFailureIn` mean.
    const body: SimulateFailureRequest = {};
    if (scenario !== ANY) body.scenario = scenario;
    if (amountPaise !== null) body.amount_paise = amountPaise;
    if (method !== ANY) body.method = method;

    try {
      await api.simulateFailure(body);
      setSubmitting(false);
      setOpen(false);
      onSimulated();
    } catch (caught) {
      // Surfaced in the dialog rather than swallowed. A scenario id the seed
      // catalogue does not contain, or a backend that has gone away mid-demo,
      // has to be visible - silently closing on failure would leave a reviewer
      // waiting for a row that is never going to appear.
      setError(errorMessage(caught));
      setSubmitting(false);
    }
  }

  return (
    <>
      <Button
        className={className}
        leadingIcon={<CirclePlus className="h-4 w-4" />}
        onClick={() => setOpen(true)}
      >
        Simulate a failed payment
      </Button>

      <Dialog
        open={open}
        onOpenChange={(next) => (next ? setOpen(true) : close())}
        title="Simulate a failed payment"
        description="Creates a failed payment through the same ingestion path a Razorpay webhook uses. The classifier, the propensity model and every guardrail then run on it for real."
        footer={
          <DialogFooter>
            <Button variant="ghost" onClick={close} disabled={submitting}>
              Cancel
            </Button>
            <Button loading={submitting} loadingText="Creating…" onClick={() => void submit()}>
              Create failed payment
            </Button>
          </DialogFooter>
        }
      >
        <div className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="simulate-scenario" className="block text-xs font-medium">
              Failure scenario
            </label>
            <select
              id="simulate-scenario"
              className={FIELD_CLASSES}
              value={scenario}
              disabled={submitting}
              onChange={(event) => setScenario(event.target.value)}
            >
              <option value={ANY}>Random - drawn from the seeded catalogue</option>
              {scenarios.map((option) => (
                <option key={option.key} value={option.key}>
                  {option.label} ({FAILURE_CATEGORY_LABEL[option.expected_category]})
                </option>
              ))}
            </select>
            {scenariosError === null ? null : (
              <p className="text-2xs text-slate-500 dark:text-slate-400">
                Could not load the scenario list ({scenariosError}). &ldquo;Random&rdquo; still works.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <label htmlFor="simulate-amount" className="block text-xs font-medium">
              Amount
            </label>
            <select
              id="simulate-amount"
              className={FIELD_CLASSES}
              // `null` has no representation in a DOM value, so the index is
              // used as the option key and the preset is looked up on change.
              value={String(AMOUNT_PRESETS.findIndex((preset) => preset.paise === amountPaise))}
              disabled={submitting}
              onChange={(event) => {
                const preset = AMOUNT_PRESETS[Number(event.target.value)];
                setAmountPaise(preset ? preset.paise : null);
              }}
            >
              {AMOUNT_PRESETS.map((preset, index) => (
                <option key={preset.label} value={String(index)}>
                  {preset.hint ? `${preset.label} - ${preset.hint}` : preset.label}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="simulate-method" className="block text-xs font-medium">
              Payment method
            </label>
            <select
              id="simulate-method"
              className={FIELD_CLASSES}
              value={method}
              disabled={submitting}
              // Matched against the known list rather than cast: `value` is a
              // plain string as far as the DOM is concerned, and asserting it
              // into the enum would let any future markup change through.
              onChange={(event) => {
                const chosen = SELECTABLE_METHODS.find((option) => option === event.target.value);
                setMethod(chosen ?? ANY);
              }}
            >
              <option value={ANY}>Match the scenario</option>
              {SELECTABLE_METHODS.map((value) => (
                <option key={value} value={value}>
                  {PAYMENT_METHOD_LABEL[value]}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              The method changes which recovery strategies are even available - a broken card path
              is the case for switching to UPI, and the reverse.
            </p>
          </div>

          {error ? (
            <Alert variant="danger">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}
        </div>
      </Dialog>
    </>
  );
}
