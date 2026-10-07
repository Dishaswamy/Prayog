"""Audit helper over the platform's single append-only log (FR-INT-06).

Records carry identifiers, outcome and a short reason only, never content (FR-INT-07):
no claims, log text, prompts or responses. Steward activity is attributed to the Steward
service principal (FR-AUTH-73) so every agent action is traceable (FR-GPU-153, NFR-SEC-007).
"""

from __future__ import annotations

from dataclasses import dataclass

from prayog_steward.clock import Clock
from prayog_steward.ports import AuditEvent, AuditSink, Principal, PrincipalKind


@dataclass
class Auditor:
    sink: AuditSink
    clock: Clock
    service_principal_id: str = "svc:steward"

    def by_steward(
        self, operation: str, *resources: str, outcome: str = "ok", reason: str | None = None
    ) -> None:
        self._append(
            self.service_principal_id, PrincipalKind.SERVICE, operation, resources, outcome, reason
        )

    def by(
        self,
        principal: Principal,
        operation: str,
        *resources: str,
        outcome: str = "ok",
        reason: str | None = None,
    ) -> None:
        self._append(principal.principal_id, principal.kind, operation, resources, outcome, reason)

    def by_user_id(
        self,
        user_id: str,
        operation: str,
        *resources: str,
        outcome: str = "ok",
        reason: str | None = None,
    ) -> None:
        self._append(user_id, PrincipalKind.USER, operation, resources, outcome, reason)

    def _append(
        self,
        actor: str,
        kind: PrincipalKind,
        operation: str,
        resources: tuple[str, ...],
        outcome: str,
        reason: str | None,
    ) -> None:
        self.sink.append(
            AuditEvent(
                actor=actor,
                actor_kind=kind,
                operation=operation,
                resource_ids=tuple(resources),
                outcome=outcome,
                at=self.clock.now(),
                reason=reason[:200] if reason else None,
            )
        )
