"""Physical instance identity shared by transport, persistence and evaluation."""

from collections.abc import Iterable, Mapping

RequestIdentity = tuple[str, str, str]

from ..errors import DFMError


def measurement_instance_id(quality: Mapping | None) -> str | None:
    if quality is None:
        return None
    if not isinstance(quality, Mapping):
        raise DFMError(
            "geometric_result_invalid", "Measurement quality must be an object."
        )
    if "measurement_instance_id" not in quality:
        return None
    instance = quality["measurement_instance_id"]
    if not isinstance(instance, str) or not instance.strip():
        raise DFMError(
            "geometric_result_invalid",
            "A physical measurement instance requires a non-empty identity.",
        )
    return instance


class ResultCoverage:
    """Require complete request coverage and unambiguous per-request outcomes.

    A request yields one unscoped outcome, or named physical instances. Mixing
    those cardinalities, duplicate instances, and unsolicited work are errors.
    """

    def __init__(self, requested: Iterable[RequestIdentity]) -> None:
        self.requested = set(requested)
        self.instances: dict[RequestIdentity, set[str | None]] = {}

    def accept(self, request: RequestIdentity, quality: Mapping | None) -> None:
        instance = measurement_instance_id(quality)
        seen = self.instances.setdefault(request, set())
        if (
            request not in self.requested
            or instance in seen
            or (seen and (instance is None or None in seen))
        ):
            raise DFMError(
                "geometry_protocol_invalid",
                "A Worker result has an unrequested or ambiguous measurement identity.",
                {
                    "feature_id": request[0],
                    "geometric_id": request[1],
                    "measurement_instance_id": instance,
                },
            )
        seen.add(instance)

    def require_complete(self) -> None:
        missing = self.requested - self.instances.keys()
        if missing:
            raise DFMError(
                "geometry_protocol_invalid",
                "The Worker omitted one or more submitted Geometric results.",
                {"missing_count": len(missing)},
            )
