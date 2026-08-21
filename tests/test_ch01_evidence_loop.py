import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from examples.ch01_evidence_loop import (  # noqa: E402
    ActionType,
    BeliefState,
    Evidence,
    EvidenceStatus,
    Observation,
    PowerProfile,
    TaskSession,
    add_demo_device_evidence,
    add_label_evidence,
    build_demo_belief,
    evaluate_compatibility,
    parse_power_profiles,
    plan_next_actions,
)


class Chapter01EvidenceLoopTests(unittest.TestCase):
    def complete_belief(
        self,
        label_text: str = "USB Power Delivery: 5V/3A, 9V/3A, 20V/3.25A",
        *,
        source_port: str = "USB-C",
        source_protocol: str | None = None,
    ) -> BeliefState:
        belief = build_demo_belief()
        if source_port != "USB-C":
            belief = BeliefState(target_id="charger-01")
            belief.add_evidence(
                Evidence(
                    evidence_id="ev-source-port",
                    target_id=belief.target_id,
                    field="source_port_type",
                    value=source_port,
                    source_type="visible_feature",
                    source_id="obs-port",
                    confidence=0.99,
                )
            )
        add_demo_device_evidence(belief)
        add_label_evidence(
            belief,
            Observation(
                observation_id="obs-label",
                target_id=belief.target_id,
                view_type="back_label",
                quality_score=0.9,
            ),
            label_text,
        )
        if source_protocol is not None:
            protocol_evidence = belief.confirmed["supported_protocol"]
            belief = self._replace_confirmed(
                belief,
                "supported_protocol",
                Evidence(
                    evidence_id="ev-protocol-override",
                    target_id=belief.target_id,
                    field="supported_protocol",
                    value=source_protocol,
                    source_type="manual_spec",
                    source_id="charger-manual",
                    confidence=1.0,
                    derived_from=(protocol_evidence.evidence_id,),
                ),
            )
        return belief

    @staticmethod
    def _replace_confirmed(
        belief: BeliefState,
        field_name: str,
        evidence: Evidence,
    ) -> BeliefState:
        replacement = BeliefState(target_id=belief.target_id)
        for existing in belief.ledger.values():
            if existing.field != field_name:
                replacement.add_evidence(existing)
        replacement.add_evidence(evidence)
        replacement.observed_views.update(belief.observed_views)
        return replacement

    def test_task_session_uses_same_thread_id_in_mvp(self) -> None:
        session = TaskSession("session-1", "compatibility_check", "charger-01")

        self.assertEqual(session.thread_id, session.session_id)
        with self.assertRaises(ValueError):
            TaskSession("session-1", "compatibility_check", "charger-01", thread_id="other")

    def test_missing_evidence_requests_back_label_and_device_model(self) -> None:
        actions = plan_next_actions(build_demo_belief())

        self.assertEqual(
            [action.action_type for action in actions],
            [ActionType.REQUEST_VIEW, ActionType.ASK_USER],
        )
        self.assertEqual(actions[0].payload["view_type"], "back_label")

    def test_label_profiles_are_parsed_and_maximum_power_is_derived(self) -> None:
        belief = build_demo_belief()
        observation = Observation("obs-label", belief.target_id, "back_label", 0.9)

        profiles = add_label_evidence(
            belief,
            observation,
            "USB-PD: 5V/3A, 9V/3A, 20V/3.25A",
        )

        self.assertEqual(parse_power_profiles("5V/3A, 9V/3A, 20V/3.25A"), profiles)
        self.assertEqual(max(item.power_w for item in profiles), 65.0)
        profile_evidence = belief.confirmed["power_profiles"]
        max_power_evidence = belief.confirmed["maximum_output_power_w"]
        self.assertEqual(profile_evidence.derived_from, ("ev-obs-label-ocr",))
        self.assertEqual(max_power_evidence.derived_from, (profile_evidence.evidence_id,))

    def test_observation_target_mismatch_is_rejected_without_mutation(self) -> None:
        belief = build_demo_belief()
        observation = Observation("obs-other", "charger-02", "back_label", 0.9)

        with self.assertRaises(ValueError):
            add_label_evidence(belief, observation, "USB-PD: 20V/3.25A")

        self.assertNotIn("back_label", belief.observed_views)
        self.assertNotIn("back_label_ocr_text", belief.confirmed)

    def test_label_without_protocol_keeps_protocol_unknown(self) -> None:
        belief = build_demo_belief()
        add_demo_device_evidence(belief)
        observation = Observation("obs-label", belief.target_id, "back_label", 0.9)

        add_label_evidence(belief, observation, "Output: 5V/3A, 20V/3.25A")
        result = evaluate_compatibility(belief)

        self.assertEqual(belief.state_of("supported_protocol"), EvidenceStatus.UNKNOWN)
        self.assertIsNone(result.meets_mvp_charging_requirements)
        self.assertEqual(result.unknowns, ("supported_protocol",))

    def test_matching_65w_profile_meets_mvp_rule(self) -> None:
        result = evaluate_compatibility(self.complete_belief())

        self.assertTrue(result.meets_mvp_charging_requirements)
        self.assertEqual(result.decision, "meets_mvp_charging_requirements")
        self.assertEqual(result.power_status, "sufficient")
        self.assertTrue(any("obs-label" in item for item in result.evidence_references))

    def test_30w_profile_does_not_meet_45w_requirement(self) -> None:
        result = evaluate_compatibility(
            self.complete_belief("USB Power Delivery: 5V/3A, 9V/3A, 20V/1.5A")
        )

        self.assertFalse(result.meets_mvp_charging_requirements)
        self.assertEqual(result.decision, "does_not_meet_mvp_charging_requirements")
        self.assertEqual(result.power_status, "insufficient")

    def test_high_maximum_without_required_voltage_profile_fails(self) -> None:
        result = evaluate_compatibility(
            self.complete_belief("USB Power Delivery: 5V/3A, 13V/5A")
        )

        self.assertFalse(result.meets_mvp_charging_requirements)
        self.assertEqual(result.power_status, "required_voltage_profile_missing")

    def test_port_mismatch_does_not_meet_mvp_rule(self) -> None:
        result = evaluate_compatibility(self.complete_belief(source_port="USB-A"))

        self.assertFalse(result.meets_mvp_charging_requirements)
        self.assertEqual(result.port_status, "incompatible")

    def test_protocol_mismatch_does_not_meet_mvp_rule(self) -> None:
        result = evaluate_compatibility(self.complete_belief(source_protocol="QC"))

        self.assertFalse(result.meets_mvp_charging_requirements)
        self.assertEqual(result.protocol_status, "incompatible")

    def test_conflicting_power_profiles_block_a_conclusion(self) -> None:
        belief = self.complete_belief()
        belief.add_evidence(
            Evidence(
                evidence_id="ev-profiles-conflict",
                target_id=belief.target_id,
                field="power_profiles",
                value=(PowerProfile(20, 1.5, 30),),
                source_type="manual_spec",
                source_id="conflicting-source",
                confidence=0.8,
            )
        )

        result = evaluate_compatibility(belief)

        self.assertIsNone(result.meets_mvp_charging_requirements)
        self.assertEqual(result.decision, "insufficient_evidence")
        self.assertIn("power_profiles", result.unknowns)


class BeliefStateTransitionTests(unittest.TestCase):
    def evidence(
        self,
        evidence_id: str,
        value: object,
        status: EvidenceStatus = EvidenceStatus.CONFIRMED,
    ) -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            target_id="charger-01",
            field="supported_protocol",
            value=value,
            source_type="test",
            source_id=evidence_id,
            confidence=1.0,
            status=status,
        )

    def test_confirmed_then_unknown_stays_only_confirmed(self) -> None:
        belief = BeliefState("charger-01")
        belief.add_evidence(self.evidence("ev-confirmed", "USB-PD"))
        belief.add_evidence(self.evidence("ev-unknown", None, EvidenceStatus.UNKNOWN))

        self.assertEqual(belief.state_of("supported_protocol"), EvidenceStatus.CONFIRMED)
        self.assertNotIn("supported_protocol", belief.unknown)

    def test_same_confirmed_value_merges_supporting_sources(self) -> None:
        belief = BeliefState("charger-01")
        belief.add_evidence(self.evidence("ev-one", "USB-PD"))
        belief.add_evidence(self.evidence("ev-two", "USB-PD"))

        self.assertEqual(
            [item.evidence_id for item in belief.evidence_for("supported_protocol")],
            ["ev-one", "ev-two"],
        )

    def test_probable_then_confirmed_promotes_to_confirmed(self) -> None:
        belief = BeliefState("charger-01")
        belief.add_evidence(self.evidence("ev-probable", "USB-PD", EvidenceStatus.PROBABLE))
        belief.add_evidence(self.evidence("ev-confirmed", "USB-PD"))

        self.assertEqual(belief.state_of("supported_protocol"), EvidenceStatus.CONFIRMED)
        self.assertNotIn("supported_protocol", belief.probable)

    def test_explicit_conflict_enters_conflict_state(self) -> None:
        belief = BeliefState("charger-01")
        belief.add_evidence(self.evidence("ev-conflict", "ambiguous", EvidenceStatus.CONFLICT))

        self.assertEqual(belief.state_of("supported_protocol"), EvidenceStatus.CONFLICT)
        self.assertNotIn("supported_protocol", belief.confirmed)

    def test_conflict_is_sticky_until_explicit_revalidation(self) -> None:
        belief = BeliefState("charger-01")
        belief.add_evidence(self.evidence("ev-one", "USB-PD"))
        belief.add_evidence(self.evidence("ev-two", "QC"))
        belief.add_evidence(self.evidence("ev-three", "USB-PD"))

        self.assertEqual(belief.state_of("supported_protocol"), EvidenceStatus.CONFLICT)
        self.assertEqual(len(belief.conflicts["supported_protocol"]), 3)


if __name__ == "__main__":
    unittest.main()
