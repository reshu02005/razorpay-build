"""
The payment gateway abstraction: one interface, two transports.

Why this module exists
----------------------
RecoverAI must run for a reviewer who has no Razorpay account, and it must run
against real Razorpay Test Mode for a demo. The naive way to satisfy both is an
``if simulated:`` branch inside the recovery service. That would be a lie: the
demo would exercise a code path that production never runs, so a green demo
would prove nothing about the real integration.

Instead the service layer is written exactly once against ``PaymentGateway``.
``RazorpayGateway`` and ``SimulatedGateway`` differ only in *transport* -- HTTP
versus an in-process dictionary. Everything above them (order creation,
signature verification, state transitions, the audit ledger) is byte-for-byte
the same code in both modes.

The signature check is where that promise is easiest to break and most important
to keep. A simulator that returned ``True`` from ``verify_payment_signature``
would make the demo pass while proving nothing -- the single most security-
relevant line in the product would be untested. So the simulator computes a
genuine HMAC-SHA256 with a fixed local secret and hands back a real signature,
and the server verifies it with the identical function that verifies Razorpay's.
The only thing the simulator fakes is who holds the secret.

Units
-----
``amount_paise`` is an integer, and it is what goes on the wire: Razorpay's
Orders API takes ``amount`` in the currency's smallest unit. Storing paise
internally means there is no conversion at the API boundary -- which is exactly
the boundary where unit-conversion mistakes turn into real money moving.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import string
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from app.config import Settings
from app.domain.enums import GatewayMode
from app.domain.errors import ConfigurationError, GatewayError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
# These are frozen dataclasses rather than Pydantic models on purpose: they never
# cross the HTTP boundary (the service maps them into the ``*Out`` schemas), so
# paying for Pydantic validation here would buy nothing. Frozen means a gateway
# response cannot be mutated after the fact by the code that consumes it.


@dataclass(frozen=True)
class GatewayOrder:
    """An order the customer can pay against. The unit of a recovery attempt."""

    id: str
    amount_paise: int
    currency: str
    receipt: str
    status: str


@dataclass(frozen=True)
class GatewayPayment:
    """A payment made against an order, as the gateway currently sees it."""

    id: str
    order_id: str
    amount_paise: int
    status: str
    method: str


# ---------------------------------------------------------------------------
# The signature algorithm -- defined once, used by both implementations
# ---------------------------------------------------------------------------


def compute_payment_signature(*, order_id: str, payment_id: str, secret: str) -> str:
    """
    Razorpay's checkout signature: ``HMAC_SHA256(secret, "<order_id>|<payment_id>")``.

    Args:
        order_id: The order the payment was made against.
        payment_id: The payment id Checkout returned to the browser.
        secret: The key secret. Never leaves the server.

    Returns:
        The lowercase hex digest Razorpay's own client libraries produce.

    This lives at module level, not on either class, because "both gateways run
    the identical algorithm" is a property of the module. If it were a method on
    each class, the two copies could drift, and a drifted simulator would quietly
    stop testing the production verifier.
    """
    # The pipe-joined string is Razorpay's spec, not our invention. The order of
    # the two ids matters -- swapping them produces a different, valid-looking
    # digest that will never match.
    message = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _signature_matches(*, order_id: str, payment_id: str, signature: str, secret: str) -> bool:
    """
    Constant-time comparison of a claimed signature against the expected one.

    Args:
        order_id, payment_id: The pair that was signed.
        signature: The value the client claims Razorpay produced.
        secret: The key secret to verify against.

    Returns:
        True only when the claimed signature is exactly the expected digest.
    """
    # An unset secret must never verify. Returning False (rather than raising)
    # keeps a misconfigured deployment in "nothing verifies" rather than
    # "everything verifies", which is the safe direction to fail.
    if not secret or not signature:
        return False

    expected = compute_payment_signature(order_id=order_id, payment_id=payment_id, secret=secret)

    try:
        # hmac.compare_digest, never ==. A plain string comparison short-circuits
        # on the first differing byte, so the time it takes leaks how many
        # leading characters were correct; an attacker can recover a valid
        # signature byte by byte from timing alone. compare_digest always takes
        # the same time regardless of where the strings differ.
        return hmac.compare_digest(expected, signature)
    except TypeError:
        # compare_digest rejects str arguments containing non-ASCII characters.
        # `signature` arrives from an HTTP request, so an attacker chooses its
        # bytes; a TypeError escaping here would turn a bad signature into a 500.
        return False


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


@runtime_checkable
class PaymentGateway(Protocol):
    """
    What the recovery service is allowed to ask a payment backend to do.

    Deliberately tiny. There is no refund, no capture, no customer-charge method:
    the service can create something payable and read the result, and that is
    all. Narrowing the interface is a security control, not minimalism for its
    own sake -- an operation absent from this protocol cannot be reached by any
    code above it, including by the agent.

    ``@runtime_checkable`` lets tests assert ``isinstance(gw, PaymentGateway)``.
    Note that ``issubclass`` still raises for protocols with non-method members
    such as ``mode``; only ``isinstance`` is supported.
    """

    #: Which backend is live. Rendered in the UI so a demo can never imply that
    #: simulated money moved through Razorpay.
    mode: GatewayMode

    #: The publishable key id, or None in simulated mode. Safe to send to the
    #: browser -- Razorpay Checkout needs it; the secret never leaves the server.
    key_id: str | None

    def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
    ) -> GatewayOrder:
        """Create a payable order for exactly ``amount_paise``."""
        ...

    def fetch_payment(self, payment_id: str) -> GatewayPayment:
        """Read a payment's current state from the gateway (server-side truth)."""
        ...

    def verify_payment_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        """Return True only if this order/payment pair was genuinely signed."""
        ...


# ---------------------------------------------------------------------------
# Real transport
# ---------------------------------------------------------------------------


class RazorpayGateway:
    """
    Razorpay REST API over HTTPS, authenticated with HTTP Basic.

    Razorpay's own Python SDK is a supported alternative and was considered. It
    lost for two reasons: it adds a dependency whose only job is to wrap
    ``requests``, and it raises its own exception hierarchy, which would leak a
    third-party error type into our service layer. Using ``httpx`` directly means
    the project has exactly one HTTP client (the FastAPI test client uses it too)
    and exactly one error type crossing this boundary.
    """

    #: Test-mode and live-mode share a host; the key prefix selects the account.
    #: ``config.Settings`` refuses to boot on an ``rzp_live_`` key, so this class
    #: does not re-check that -- the interlock lives at exactly one layer.
    API_BASE = "https://api.razorpay.com/v1"

    mode: GatewayMode = GatewayMode.RAZORPAY_TEST

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: Application settings carrying the Razorpay credentials.

        Raises:
            ConfigurationError: If either credential is missing. Constructing a
                real gateway without credentials is a programming error, not a
                degraded mode -- the degraded mode is ``SimulatedGateway``, and
                ``get_gateway`` is what chooses between them. Silently falling
                back here would hide a broken ``.env`` from the operator.
        """
        if not settings.razorpay_enabled:
            raise ConfigurationError(
                "RazorpayGateway needs both RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET. "
                "Leave both blank to run on the simulated gateway instead."
            )
        self.key_id: str | None = settings.razorpay_key_id
        self._key_secret = settings.razorpay_key_secret
        #: Every call carries this explicitly. Relying on the client library's
        #: default timeout is how a payment call ends up hanging a request thread
        #: for minutes; making it a named setting also makes it tunable without
        #: touching code.
        self._timeout_seconds = settings.razorpay_timeout_seconds

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Perform one authenticated call and return the decoded JSON body.

        Args:
            method: HTTP verb.
            path: Path below ``API_BASE``, starting with "/".
            json_body: Optional JSON request body.

        Returns:
            The parsed response object.

        Raises:
            GatewayError: For transport failures, non-2xx responses and
                undecodable bodies alike. The service layer above deals with one
                error type; translating here means it never has to know that
                ``httpx.ConnectError``, ``httpx.ReadTimeout`` and an HTTP 400 are
                three different things.
        """
        url = f"{self.API_BASE}{path}"

        try:
            # A short-lived client per call rather than one long-lived pooled
            # client. A module-level client would save a TCP handshake, but it
            # would also need explicit shutdown wiring in the FastAPI lifespan
            # and would keep sockets open across test cases. At this call volume
            # -- a handful of orders per demo -- correctness of lifecycle beats
            # connection reuse.
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.request(
                    method,
                    url,
                    json=json_body,
                    auth=(self.key_id or "", self._key_secret),
                )
        except httpx.HTTPError as exc:
            # Covers timeouts, DNS failures, TLS errors and connection resets.
            raise GatewayError(
                f"Could not reach Razorpay: {type(exc).__name__}.",
                detail={"path": path, "reason": str(exc)},
            ) from exc

        if response.status_code >= 400:
            # Razorpay returns {"error": {"code", "description", "reason", ...}}.
            # Surfacing their own description is what makes a failed order
            # actionable ("Amount exceeds maximum permitted") instead of an
            # opaque "gateway error 400" the operator cannot do anything about.
            description: str | None = None
            error_code: str | None = None
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            if isinstance(error_body, dict):
                error_obj = error_body.get("error")
                if isinstance(error_obj, dict):
                    raw_description = error_obj.get("description")
                    raw_code = error_obj.get("code")
                    description = raw_description if isinstance(raw_description, str) else None
                    error_code = raw_code if isinstance(raw_code, str) else None
            raise GatewayError(
                description or f"Razorpay rejected the request with HTTP {response.status_code}.",
                detail={
                    "path": path,
                    "http_status": response.status_code,
                    "razorpay_code": error_code,
                },
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(
                "Razorpay returned a body that is not JSON.",
                detail={"path": path, "http_status": response.status_code},
            ) from exc

        if not isinstance(payload, dict):
            raise GatewayError(
                "Razorpay returned an unexpected JSON shape.",
                detail={"path": path, "type": type(payload).__name__},
            )
        return payload

    # -- interface ---------------------------------------------------------

    def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
    ) -> GatewayOrder:
        """
        Create a Razorpay order the customer can pay against.

        Args:
            amount_paise: Amount in paise. Sent verbatim -- Razorpay's ``amount``
                field is already the smallest currency unit, which is why the
                whole system stores paise: no conversion happens at this
                boundary, so no rounding error can be introduced at it.
            currency: ISO currency code (INR for this project).
            receipt: Merchant-side reference; we pass the recovery attempt id so
                a Razorpay dashboard row can be traced back to a case.
            notes: Free-form key/value metadata echoed back on the order and in
                webhooks. Used to carry the case id.

        Returns:
            The created order.

        Raises:
            GatewayError: If Razorpay refuses or is unreachable.
        """
        body: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "notes": notes,
            # Auto-capture on authorisation. The alternative -- authorise now,
            # capture later -- would leave money held on the customer's card
            # while a second system decided whether to take it. A recovery is a
            # single decision that has already passed human approval, so there is
            # nothing left to decide between authorisation and capture.
            "payment_capture": 1,
        }
        data = self._request("POST", "/orders", json_body=body)
        return GatewayOrder(
            id=str(data.get("id") or ""),
            # Read the amount back from the response rather than echoing the
            # request: if Razorpay ever disagreed with us about the amount, the
            # audit trail must record what Razorpay actually created.
            amount_paise=int(data.get("amount") or amount_paise),
            currency=str(data.get("currency") or currency),
            receipt=str(data.get("receipt") or receipt),
            status=str(data.get("status") or "created"),
        )

    def fetch_payment(self, payment_id: str) -> GatewayPayment:
        """
        Read a payment's server-side state.

        Args:
            payment_id: Razorpay payment id (``pay_...``).

        Returns:
            The payment as Razorpay currently reports it.

        Raises:
            GatewayError: If the payment is unknown or Razorpay is unreachable.
        """
        # The id can originate in a client-supplied request body, so it is
        # percent-encoded before being interpolated into the path. Without
        # ``safe=""`` a caller could inject "../" and address a different
        # endpoint on the same host.
        data = self._request("GET", f"/payments/{quote(payment_id, safe='')}")
        return GatewayPayment(
            id=str(data.get("id") or payment_id),
            order_id=str(data.get("order_id") or ""),
            amount_paise=int(data.get("amount") or 0),
            status=str(data.get("status") or "unknown"),
            method=str(data.get("method") or "unknown"),
        )

    def verify_payment_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        """
        Verify the signature Razorpay Checkout handed to the browser.

        Args:
            order_id: The order that was paid.
            payment_id: The payment Checkout reported.
            signature: The ``razorpay_signature`` value the browser posted back.

        Returns:
            True only when the signature is genuine.

        The browser's "payment succeeded" callback is a claim by an untrusted
        client; this HMAC is the proof. Without it, anyone could mark any case
        recovered with a single curl request.
        """
        return _signature_matches(
            order_id=order_id,
            payment_id=payment_id,
            signature=signature,
            secret=self._key_secret,
        )


# ---------------------------------------------------------------------------
# Simulated transport
# ---------------------------------------------------------------------------

#: The secret the simulator signs with. A fixed constant, not a random value
#: generated at start-up, so a signature stays verifiable across a server reload
#: -- a demo where the checkout page stops verifying after an autoreload would be
#: a confusing artefact of the simulator rather than a real behaviour.
#:
#: It is not sensitive: it protects nothing, because no simulated payment moves
#: money. It is named ``simulated`` in full so that it can never be mistaken for
#: a leaked production secret in a code search.
SIMULATED_KEY_SECRET = "recoverai_simulated_key_secret"

#: Razorpay ids are a prefix plus 14 alphanumeric characters. Matching that shape
#: is not cosmetic: screenshots and audit rows look real, and any code that
#: accidentally assumes a particular id length or character set is exercised in
#: simulated mode instead of failing for the first time against production.
_ID_ALPHABET = string.ascii_uppercase + string.digits
_ID_LENGTH = 14


def _simulated_id(prefix: str) -> str:
    """Generate an identifier shaped like Razorpay's (``order_``/``pay_`` + 14 chars)."""
    # ``secrets`` rather than ``random``: the ML dataset generator seeds the
    # global ``random`` state for reproducibility, and drawing ids from that same
    # generator would both perturb its sequence and make ids depend on whether a
    # model had been trained in the same process.
    body = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
    return f"{prefix}_{body}"


class SimulatedGateway:
    """
    In-process payment backend for running with zero credentials.

    It holds orders and payments in dictionaries and performs no I/O, so the
    whole product -- dashboard, approval, checkout, verification, audit chain --
    works on a laptop with no network and no Razorpay account.

    What it does *not* fake is verification. ``simulate_payment`` returns a real
    HMAC-SHA256 signature over the real id pair, and ``verify_payment_signature``
    runs the same module-level function the Razorpay implementation runs. The
    checkout screen therefore exercises genuine server-side signature
    verification; only the party holding the secret is different.

    State is per-instance and lost on restart. Persisting it was considered and
    rejected: the durable record of a recovery is the ``recovery_attempts`` table
    and the audit ledger, and a second, parallel store of gateway state would be
    a second thing that could disagree with the database.
    """

    mode: GatewayMode = GatewayMode.SIMULATED

    #: No publishable key exists, and the checkout page keys off exactly this to
    #: render the simulated pay/fail buttons instead of loading Razorpay's SDK.
    key_id: str | None = None

    def __init__(self) -> None:
        self._orders: dict[str, GatewayOrder] = {}
        self._payments: dict[str, GatewayPayment] = {}

    def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
    ) -> GatewayOrder:
        """
        Create an in-memory order.

        Args:
            amount_paise: Amount in paise, stored unchanged.
            currency: ISO currency code.
            receipt: Merchant-side reference.
            notes: Metadata, accepted and ignored -- the real gateway echoes them
                back on webhooks, which this simulator has no need to reproduce.

        Returns:
            The created order, in the state Razorpay would report ("created").
        """
        order = GatewayOrder(
            id=_simulated_id("order"),
            amount_paise=amount_paise,
            currency=currency,
            receipt=receipt,
            status="created",
        )
        self._orders[order.id] = order
        return order

    def fetch_payment(self, payment_id: str) -> GatewayPayment:
        """
        Read back a payment produced by ``simulate_payment``.

        Args:
            payment_id: A ``pay_...`` id this instance issued.

        Returns:
            The simulated payment.

        Raises:
            GatewayError: If the id is unknown -- the same error type the real
                gateway raises for an unknown payment, so callers need no
                mode-specific handling.
        """
        payment = self._payments.get(payment_id)
        if payment is None:
            raise GatewayError(
                f"No simulated payment with id '{payment_id}'.",
                detail={"payment_id": payment_id, "gateway_mode": GatewayMode.SIMULATED.value},
            )
        return payment

    def verify_payment_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        """
        Verify a signature produced by ``simulate_payment``.

        Args:
            order_id: The order that was paid.
            payment_id: The simulated payment id.
            signature: The signature the checkout page posted back.

        Returns:
            True only when the HMAC matches.

        This is the identical function ``RazorpayGateway`` calls, with a different
        secret. That is the point of the simulator: the verification code proved
        by the credential-free demo is the verification code that runs in
        production.
        """
        return _signature_matches(
            order_id=order_id,
            payment_id=payment_id,
            signature=signature,
            secret=SIMULATED_KEY_SECRET,
        )

    def simulate_payment(
        self, order_id: str, *, amount_paise: int, succeed: bool = True
    ) -> tuple[str, str]:
        """
        Play the role of the customer completing (or abandoning) checkout.

        Args:
            order_id: The order being paid. It does **not** have to be one this
                instance created -- see below.
            amount_paise: The amount, supplied by the caller.
            succeed: Whether the simulated customer paid successfully.

        Returns:
            ``(payment_id, signature)``. On success the signature is a genuine
            HMAC over ``"<order_id>|<payment_id>"``, so posting the pair to the
            verify endpoint runs real server-side verification.

            On failure the signature is the empty string. Razorpay only ever
            signs a successful checkout, so issuing a valid signature for a
            failed payment would be a fiction -- and a dangerous one: a bug in
            the failure path could then verify successfully and mark a case
            recovered for money that was never collected. An empty signature
            provably fails verification, which is the honest outcome.

        The amount is a parameter rather than a lookup on ``self._orders``, and
        that is the whole point. This simulator holds its orders in process
        memory, so a backend restart -- or a ``--reload`` cycle after any file
        save -- used to make every already-approved case unpayable: the checkout
        page rendered fine and then the Pay button returned a 502 for an order the
        gateway had forgotten. Combined with the duplicate-order guardrail, the
        case had no way back.
        
        The caller already holds the authoritative amount on the case row, so
        taking it as an argument removes the dependency on in-memory state
        entirely. The order dictionary is still maintained for anything that wants
        to read order status, but nothing depends on it surviving a restart.

        Raises:
            Nothing. An unknown order is registered on the spot rather than
            refused: the amount came from the caller, so there is nothing the
            forgotten record would have told us.
        """
        order = self._orders.get(order_id)
        if order is None:
            # Re-register what was lost. The signature is an HMAC over the ids
            # alone, so a reconstructed order verifies exactly like an original.
            logger.info(
                "Re-registering simulated order %s; the previous process did not "
                "keep it. Amounts come from the case, so nothing is lost.",
                order_id,
            )
            order = GatewayOrder(
                id=order_id,
                amount_paise=amount_paise,
                currency="INR",
                receipt="",
                status="created",
            )
            self._orders[order_id] = order

        payment_id = _simulated_id("pay")
        self._payments[payment_id] = GatewayPayment(
            id=payment_id,
            order_id=order_id,
            amount_paise=amount_paise,
            status="captured" if succeed else "failed",
            # The simulator has no instrument. Recording "unknown" rather than
            # inventing "card" keeps fiction out of the audit trail; the case
            # already records which strategy was approved.
            method="unknown",
        )

        if succeed:
            # Mirror the real order lifecycle (created -> paid) so anything that
            # reads order state sees the same progression it would in production.
            self._orders[order_id] = replace(order, status="paid")
            signature = compute_payment_signature(
                order_id=order_id,
                payment_id=payment_id,
                secret=SIMULATED_KEY_SECRET,
            )
        else:
            signature = ""

        return payment_id, signature


# Static assertion that both classes satisfy the protocol. A type checker fails
# this line the moment an implementation drifts from the interface; without it,
# a missing method would only surface at runtime, in whichever mode was not
# demoed.
_IMPLEMENTATIONS: tuple[type[PaymentGateway], ...] = (RazorpayGateway, SimulatedGateway)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GatewayKey:
    """
    Hashable cache key for ``_build_gateway``.

    ``Settings`` is a Pydantic model, and Pydantic v2 sets ``__hash__`` to None
    on any model that is not frozen. Decorating ``get_gateway(settings)`` with
    ``lru_cache`` directly would therefore raise ``TypeError: unhashable type``
    on the very first request. This key carries the credential fields (which are
    hashable, and which are what actually determine the choice of gateway) and
    smuggles the settings object through with ``compare=False`` so it takes part
    in neither equality nor the hash.
    """

    razorpay_enabled: bool
    key_id: str
    key_secret: str
    #: Excluded from equality and hashing by ``compare=False``; excluded from the
    #: repr so a credential can never be printed by an unrelated traceback.
    settings: Settings = field(compare=False, repr=False)


@lru_cache(maxsize=1)
def _build_gateway(key: _GatewayKey) -> PaymentGateway:
    """Construct the gateway for a given credential set. Cached; see ``get_gateway``."""
    if key.razorpay_enabled:
        return RazorpayGateway(key.settings)
    return SimulatedGateway()


def get_gateway(settings: Settings) -> PaymentGateway:
    """
    Return the process-wide gateway, choosing the implementation from config.

    Args:
        settings: Application settings.

    Returns:
        ``RazorpayGateway`` when both Razorpay credentials are present, otherwise
        ``SimulatedGateway``.

    Caching matters for more than speed. ``SimulatedGateway`` keeps its orders in
    instance state, so a fresh instance per request would lose the order between
    creating it and paying it -- the checkout flow would break in exactly the
    mode a credential-free reviewer runs.
    """
    return _build_gateway(
        _GatewayKey(
            razorpay_enabled=settings.razorpay_enabled,
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret,
            settings=settings,
        )
    )


def reset_gateway() -> None:
    """
    Drop the cached gateway.

    For tests: one test may configure credentials and another may not, and each
    needs a simulator with empty order state rather than the previous test's.
    """
    _build_gateway.cache_clear()
