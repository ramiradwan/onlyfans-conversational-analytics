"""Fixed protected routes for native text-analysis acquisition and delivery."""
from dataclasses import dataclass

from app.security.signed_objects import decode
from app.security.text_analysis_contract import canonical, parse_json
from app.security.text_analysis_proof import ProofContext, prepare_request, sign_request
from app.security.text_analysis_reissue import TextAnalysisReissueError
from app.security.text_analysis_reissue_transport import TextAnalysisReissueHTTPTransport, _origin


class NativeAcquisitionUnavailable(OSError):
    """A protected exchange has no usable response."""


class NativeAcquisitionNotCompleted(NativeAcquisitionUnavailable):
    """The original issuance has no committed delivery yet."""


@dataclass(frozen=True)
class Route:
    family: str
    action: str
    request_limit: int
    response_limit: int

    @property
    def challenge(self) -> str:
        return f"/v1/text-analysis-{self.family}-proof-challenges"

    @property
    def operation(self) -> str:
        plural = "authority-deliveries" if self.family == "authority-delivery" else self.family + "s"
        return f"/v1/text-analysis-{plural}:{self.action}"

    @property
    def error_schema(self) -> str:
        family = "authority-issuance" if self.action == "issue" else self.family
        return f"schemas/text-analysis-{family}/v1/error.schema.json"


ROUTES = {
    **{f"{kind}-{action}": Route(f"{kind}-acquisition", action, 8192, 16384)
       for kind in ("paid", "preview") for action in ("prepare", "read", "submit")},
    **{f"{kind}-issue": Route(f"{kind}-issuance", "issue", 8192, 24576)
       for kind in ("license", "preview")},
    "authority-recover": Route("authority-delivery", "recover", 4096, 24576),
}
_BOUNDS = {
    **{route.challenge: (8192 if route.request_limit == 4096 else 12288, 4096)
       for route in ROUTES.values()},
    **{route.operation: (route.request_limit, route.response_limit) for route in ROUTES.values()},
}


class NativeAcquisitionTransport(TextAnalysisReissueHTTPTransport):
    def _bounds(self, path: str) -> tuple[int, int]:
        try:
            return _BOUNDS[path]
        except (KeyError, TypeError):
            raise NativeAcquisitionUnavailable("invalid_acquisition_path") from None


def acquisition_entry_url(origin: str, kind: str, reference: str) -> str:
    if kind not in ("paid", "preview") or len(decode(reference, 32)) != 32:
        raise ValueError("invalid_acquisition_entry")
    return f"{_origin(origin)}/onboarding/text-analysis-{kind}-acquisitions/{reference}"


class NativeAcquisitionClient:
    def __init__(self, transport, key_authority, schemas, *, clock):
        self.transport = transport
        self.key_authority = key_authority
        self.schemas = schemas
        self.clock = clock

    def exchange(self, operation, raw, binding, key, *, before_send):
        route = ROUTES[operation]
        proof = ProofContext(operation, **binding, public_key_jwk=canonical(
            parse_json(key.public_key_jwk.encode("utf-8"), maximum=1024)), expected_request=raw)
        prepared = prepare_request(raw, context=proof, schemas=self.schemas)
        try:
            challenge = self.transport.request(route.challenge, prepared.challenge_request,
                                               maximum=4096, before_send=before_send)
        except TextAnalysisReissueError:
            raise NativeAcquisitionUnavailable("challenge_transport_failed") from None
        if challenge.status_code not in (200, 201):
            raise NativeAcquisitionUnavailable("challenge_unavailable")
        signed = sign_request(raw, challenge.body, context=proof, key_authority=self.key_authority,
                              schemas=self.schemas, clock=self.clock)
        try:
            response = self.transport.request(route.operation, signed, maximum=route.response_limit,
                                              before_send=before_send)
        except TextAnalysisReissueError:
            raise NativeAcquisitionUnavailable("acquisition_transport_failed") from None
        if response.status_code in (200, 201):
            return response.body
        if operation == "authority-recover" and response.status_code == 409:
            error = parse_json(response.body, maximum=4096)
            self.schemas.require(route.error_schema, error)
            if error["reason"] == "not_completed":
                raise NativeAcquisitionNotCompleted("issuance_not_completed")
        raise NativeAcquisitionUnavailable("acquisition_unavailable")
