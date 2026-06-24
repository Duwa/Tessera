"""
HACM Dynamic Learning Engine — Tessera v2
==========================================
vs Odoo eLearning:

ODOO eLearning DOES:
  - Course catalog (static), videos, PDFs, quizzes
  - Certifications, karma points, leaderboards
  - Forums, paid courses, progress tracking

TESSERA REPLACES WITH:
  1. Zero static courses — every learning opportunity generated from live
     AI agent state (what the agent is doing RIGHT NOW)
  2. Belief-vector gap analysis: where does human diverge from org template?
  3. Three learning regimes (A/B/C) from Rajendra (2026) tripartite model
  4. Urgency calibrated to agent sophistication level
  5. "Shadow agent" and "paired task" formats (no Odoo equivalent)
  6. Learning opportunities linked to payroll (token utilization too low → learning plan)
  7. Skills Lattice integration — gaps drive specific learning recommendations
  8. Human-agent replacement risk scoring (Frontier agent + low human fitness = flag)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict
from datetime import date
import math


class LearningRegime(str, Enum):
    A = "A"   # Org-structured, batch (replaces Odoo course catalog)
    B = "B"   # Self-directed, autonomous
    C = "C"   # Operational, AI-adjacent, real-time (working = learning)


class LearningUrgency(str, Enum):
    IMMEDIATE  = "immediate"
    THIS_WEEK  = "this_week"
    THIS_MONTH = "this_month"
    GROWTH     = "growth"


class LearningFormat(str, Enum):
    SHADOW_AGENT  = "shadow_agent"   # NO ODOO EQUIVALENT
    PAIRED_TASK   = "paired_task"    # NO ODOO EQUIVALENT
    REFLECTION    = "reflection"
    WORKSHOP      = "workshop"
    SELF_STUDY    = "self_study"
    MENTORSHIP    = "mentorship"
    EXPERIMENT    = "experiment"


class AgentSophistication(str, Enum):
    BASIC    = "basic"
    CAPABLE  = "capable"
    ADVANCED = "advanced"
    FRONTIER = "frontier"


class GapType(str, Enum):
    COMPREHENSION = "comprehension"
    JUDGMENT      = "judgment"
    COLLABORATION = "collaboration"
    OVERSIGHT     = "oversight"
    DIRECTION     = "direction"
    BELIEF_DRIFT  = "belief_drift"
    REPLACEMENT   = "replacement"   # Agent doing human's job better


@dataclass
class AgentState:
    agent_id: str
    agent_name: str
    model_version: str
    fitness_score: float
    mandate_coherence: float
    rag_depth: int
    rag_activations_per_epoch: int
    failure_flag: bool
    failure_count_this_epoch: int
    decisions_participated: int
    decisions_resolved: int
    resolution_rate: float
    task_domains: List[str]
    avg_tokens_per_task: float
    sophistication: AgentSophistication = AgentSophistication.CAPABLE

    def __post_init__(self):
        score = (
            self.fitness_score * 0.3 +
            self.mandate_coherence * 0.25 +
            min(1.0, self.rag_depth / 5) * 0.2 +
            self.resolution_rate * 0.25
        )
        if score >= 0.85: self.sophistication = AgentSophistication.FRONTIER
        elif score >= 0.70: self.sophistication = AgentSophistication.ADVANCED
        elif score >= 0.50: self.sophistication = AgentSophistication.CAPABLE
        else: self.sophistication = AgentSophistication.BASIC


@dataclass
class HumanState:
    employee_id: str
    employee_name: str
    role: str
    department: str
    fitness_score: float
    activation_score: float
    belief_alignment: float
    belief_vector: List[float]
    primary_regime: LearningRegime
    hcm_level: float
    self_directed_rate: float
    token_utilization_pct: float
    agents_worked_with: List[str]
    completed_learning_this_epoch: List[str] = field(default_factory=list)


@dataclass
class LearningOpportunity:
    """
    One generated learning opportunity — no static course catalog.
    Every field is derived from live agent + human state.
    """
    opportunity_id: str
    employee_id: str
    title: str
    gap_type: GapType
    urgency: LearningUrgency
    regime: LearningRegime
    format: LearningFormat
    estimated_hours: float
    can_do_alongside_work: bool
    why_now: str                     # explains the gap in plain English
    what_you_will_be_able_to_do: str # concrete outcome
    completion_indicator: str        # how we know it worked
    suggested_resources: List[str]
    agent_id: Optional[str] = None   # which agent triggered this
    # NEW: payroll integration
    token_utilization_link: Optional[str] = None  # "Low token utilization suggests..."
    # NEW: skills lattice link
    skills_gap: Optional[str] = None
    # NEW: replacement risk score (0-1)
    replacement_risk: float = 0.0
    # Odoo equivalent (for comparison)
    odoo_equivalent: str = "hr.training (static) — no dynamic equivalent"


@dataclass
class LearningPlan:
    employee_id: str
    employee_name: str
    epoch: int
    generated_at: str
    one_line_summary: str
    primary_theme: str
    immediate_count: int
    this_week_count: int
    total_hours: float
    opportunities: List[LearningOpportunity]
    # NEW fields
    replacement_risk_score: float = 0.0   # overall replacement risk this epoch
    huang_learning_gap: float = 0.0       # token utilization gap driving learning needs
    payroll_link: str = ""                 # e.g. "Low token utilization → needs learning"


DIMS = [
    'Learning orientation', 'Change readiness', 'AI trust', 'Collaboration',
    'Oversight mindset', 'Innovation drive', 'Risk tolerance', 'Direction clarity',
    'Accountability', 'Communication', 'Strategic alignment', 'Adaptability'
]


class HACMLearningEngine:
    """
    Generates learning opportunities from live agent+human state.
    No static course catalog. Everything is contextual and dynamic.
    Odoo replaces: hr.training → completely different model.
    """

    B_STAR_PER_PERSON = 120.0

    @classmethod
    def generate_plan(
        cls,
        human: HumanState,
        agents: List[AgentState],
        org_template_vector: List[float],
        epoch: int,
    ) -> LearningPlan:
        opps = []

        # 1. TOKEN UTILIZATION LINK (payroll integration — no Odoo equivalent)
        if human.token_utilization_pct < 30:
            opps.append(cls._token_utilization_gap(human))

        # 2. BELIEF DRIFT (no Odoo equivalent)
        if human.belief_alignment < 0.55:
            opps.append(cls._belief_drift_opp(human, org_template_vector))

        # 3. DIRECTION CLARITY (dim 7)
        direction = human.belief_vector[7] if len(human.belief_vector) > 7 else 0.5
        if direction < 0.45:
            opps.append(cls._direction_clarity_opp(human))

        # 4. AGENT-SPECIFIC OPPORTUNITIES
        max_replacement_risk = 0.0
        for agent in agents:
            agent_opps, replacement_risk = cls._agent_opportunities(human, agent, org_template_vector)
            opps.extend(agent_opps)
            max_replacement_risk = max(max_replacement_risk, replacement_risk)

        # Deduplicate, sort by urgency
        urgency_order = {
            LearningUrgency.IMMEDIATE: 0,
            LearningUrgency.THIS_WEEK: 1,
            LearningUrgency.THIS_MONTH: 2,
            LearningUrgency.GROWTH: 3,
        }
        opps.sort(key=lambda o: urgency_order[o.urgency])

        # Limit to 6 (cognitive load)
        opps = opps[:6]

        immediate = sum(1 for o in opps if o.urgency == LearningUrgency.IMMEDIATE)
        this_week = sum(1 for o in opps if o.urgency == LearningUrgency.THIS_WEEK)
        total_hours = sum(o.estimated_hours for o in opps)

        # Payroll link text
        payroll_link = ""
        if human.token_utilization_pct < 40:
            payroll_link = (
                f"Token utilization is {human.token_utilization_pct:.0f}% — "
                f"below the 50% Huang minimum. Learning plan focused on closing "
                f"capability gap to enable higher utilization."
            )

        # One-line summary
        agent_names = [a.agent_name for a in agents[:2]]
        summary = cls._generate_summary(human, agents, max_replacement_risk)

        return LearningPlan(
            employee_id=human.employee_id,
            employee_name=human.employee_name,
            epoch=epoch,
            generated_at=str(date.today()),
            one_line_summary=summary,
            primary_theme=cls._primary_theme(agents, human),
            immediate_count=immediate,
            this_week_count=this_week,
            total_hours=total_hours,
            opportunities=opps,
            replacement_risk_score=max_replacement_risk,
            huang_learning_gap=max(0, 50 - human.token_utilization_pct),
            payroll_link=payroll_link,
        )

    @classmethod
    def _token_utilization_gap(cls, human: HumanState) -> LearningOpportunity:
        return LearningOpportunity(
            opportunity_id=f"TOKEN-{human.employee_id}",
            employee_id=human.employee_id,
            title="Increase AI token utilization",
            gap_type=GapType.COLLABORATION,
            urgency=LearningUrgency.THIS_WEEK,
            regime=LearningRegime.C,
            format=LearningFormat.EXPERIMENT,
            estimated_hours=3,
            can_do_alongside_work=True,
            why_now=(
                f"Your token utilization is {human.token_utilization_pct:.0f}%. "
                f"Per the Huang (2026) model, the organization is paying for AI "
                f"capability you're not using. The cost is sunk — the opportunity "
                f"is wasted."
            ),
            what_you_will_be_able_to_do=(
                "Identify 3 repeating tasks in your week that agents can assist with. "
                "Delegate them and review outputs. Build a personal delegation habit."
            ),
            completion_indicator="Token utilization above 40% next epoch.",
            suggested_resources=[
                "Review your agent's mandate — what can it do that you haven't tried?",
                "Pick your most repetitive task this week and delegate it"
            ],
            token_utilization_link=f"Payroll shows {human.token_utilization_pct:.0f}% utilization.",
            odoo_equivalent="NO_EQUIVALENT — payroll-linked learning not in Odoo"
        )

    @classmethod
    def _belief_drift_opp(cls, human: HumanState, org_template: List[float]) -> LearningOpportunity:
        # Find which dimensions diverge most
        drift_dims = []
        if len(human.belief_vector) == len(org_template) == 12:
            for i, (h, o) in enumerate(zip(human.belief_vector, org_template)):
                drift_dims.append((abs(h - o), DIMS[i]))
        drift_dims.sort(reverse=True)
        top_dims = [d[1] for d in drift_dims[:3]]
        return LearningOpportunity(
            opportunity_id=f"BELIEF-{human.employee_id}",
            employee_id=human.employee_id,
            title="Belief alignment session",
            gap_type=GapType.BELIEF_DRIFT,
            urgency=LearningUrgency.IMMEDIATE,
            regime=LearningRegime.A,
            format=LearningFormat.WORKSHOP,
            estimated_hours=3,
            can_do_alongside_work=False,
            why_now=(
                f"Your belief alignment score has fallen to {human.belief_alignment*100:.0f}%. "
                f"The dimensions drifting furthest from org direction: {', '.join(top_dims)}. "
                f"At this level, collaborative work with AI agents becomes harder — they "
                f"optimize toward org template, you're pulling differently."
            ),
            what_you_will_be_able_to_do=(
                "Articulate exactly where your perspective diverges from org direction, "
                "and why. Either align — or surface the divergence as productive dissent."
            ),
            completion_indicator="Belief alignment above 0.65 at next epoch measurement.",
            suggested_resources=["One-on-one with manager", "Belief profile review session"],
            odoo_equivalent="hr.appraisal (limited) — no belief vector equivalent in Odoo"
        )

    @classmethod
    def _direction_clarity_opp(cls, human: HumanState) -> LearningOpportunity:
        return LearningOpportunity(
            opportunity_id=f"DIR-{human.employee_id}",
            employee_id=human.employee_id,
            title="Direction quality improvement",
            gap_type=GapType.DIRECTION,
            urgency=LearningUrgency.THIS_WEEK,
            regime=LearningRegime.B,
            format=LearningFormat.EXPERIMENT,
            estimated_hours=4,
            can_do_alongside_work=False,
            why_now=(
                "Direction clarity is low. AI agents can execute far more sophisticated "
                "instructions than most people give them — you're leaving capability on "
                "the table by giving them vague or under-specified mandates."
            ),
            what_you_will_be_able_to_do=(
                "Write prompts that decompose complex tasks into agent-executable steps "
                "with appropriate scope, escalation conditions, and verification triggers."
            ),
            completion_indicator="Agent resolution rate improves >10% on your tasks.",
            suggested_resources=["Mandate review workshop", "Review your last 5 agent tasks"],
            odoo_equivalent="NO_EQUIVALENT"
        )

    @classmethod
    def _agent_opportunities(cls, human: HumanState, agent: AgentState,
                              org_template: List[float]):
        opps = []
        soph = agent.sophistication
        replacement_risk = 0.0

        # Oversight gap — always needed at Advanced/Frontier
        if soph in (AgentSophistication.ADVANCED, AgentSophistication.FRONTIER):
            urgency = LearningUrgency.IMMEDIATE if soph == AgentSophistication.FRONTIER else LearningUrgency.THIS_WEEK
            opps.append(LearningOpportunity(
                opportunity_id=f"OVERSIGHT-{human.employee_id}-{agent.agent_id}",
                employee_id=human.employee_id,
                title=f"Shadow {agent.agent_name} on complex task",
                gap_type=GapType.OVERSIGHT,
                urgency=urgency,
                regime=LearningRegime.C,
                format=LearningFormat.SHADOW_AGENT,
                estimated_hours=2,
                can_do_alongside_work=True,
                why_now=(
                    f"{agent.agent_name} is operating at {soph.value} sophistication "
                    f"(fitness {agent.fitness_score*100:.0f}%). It is now producing outputs "
                    f"in your domain that you may not be able to fully evaluate."
                ),
                what_you_will_be_able_to_do=(
                    "Identify where the agent is confident vs uncertain, and when its "
                    "output requires your verification before action."
                ),
                completion_indicator="Can reliably spot agent uncertainty signals in under 2 minutes.",
                suggested_resources=["Pick 3 recent agent outputs", "Review agent reasoning trace"],
                agent_id=agent.agent_id,
                odoo_equivalent="NO_EQUIVALENT — agent shadowing not in Odoo"
            ))

        # Replacement risk at Frontier
        if soph == AgentSophistication.FRONTIER and human.fitness_score < 0.65:
            replacement_risk = 1.0 - (human.fitness_score / 0.65)
            opps.append(LearningOpportunity(
                opportunity_id=f"REPLACE-{human.employee_id}-{agent.agent_id}",
                employee_id=human.employee_id,
                title="Judgment calibration — critical",
                gap_type=GapType.REPLACEMENT,
                urgency=LearningUrgency.IMMEDIATE,
                regime=LearningRegime.A,
                format=LearningFormat.PAIRED_TASK,
                estimated_hours=6,
                can_do_alongside_work=False,
                why_now=(
                    f"{agent.agent_name} is at Frontier level and your fitness score "
                    f"is {human.fitness_score*100:.0f}%. The agent is producing near-human "
                    f"quality outputs in your domain. Your ability to catch its errors "
                    f"is now a critical risk — not a nice-to-have."
                ),
                what_you_will_be_able_to_do=(
                    "Evaluate agent output quality without running the task yourself. "
                    "Build a personal rubric that identifies when agent confidence is "
                    "unwarranted."
                ),
                completion_indicator="Can grade agent output to within 10% of expert panel rating.",
                suggested_resources=["Create personal evaluation rubric", "Blind comparison exercises"],
                agent_id=agent.agent_id,
                replacement_risk=replacement_risk,
                odoo_equivalent="NO_EQUIVALENT"
            ))

        # Low collaboration — agent underutilized
        if human.token_utilization_pct < 50 and agent.agent_id in human.agents_worked_with:
            opps.append(LearningOpportunity(
                opportunity_id=f"COLLAB-{human.employee_id}-{agent.agent_id}",
                employee_id=human.employee_id,
                title=f"Deepen collaboration with {agent.agent_name}",
                gap_type=GapType.COLLABORATION,
                urgency=LearningUrgency.THIS_MONTH,
                regime=LearningRegime.C,
                format=LearningFormat.PAIRED_TASK,
                estimated_hours=3,
                can_do_alongside_work=True,
                why_now=(
                    f"You're working alongside {agent.agent_name} but only using "
                    f"{human.token_utilization_pct:.0f}% of its token budget. The org is paying "
                    f"for capability you're not accessing."
                ),
                what_you_will_be_able_to_do=(
                    "Identify 3 task types the agent could help with that you're currently doing alone."
                ),
                completion_indicator="Token utilization increases by at least 15 percentage points.",
                suggested_resources=["Review agent mandate", "List your top 10 repeating tasks"],
                agent_id=agent.agent_id,
                token_utilization_link=f"Current utilization: {human.token_utilization_pct:.0f}%",
                odoo_equivalent="NO_EQUIVALENT"
            ))

        return opps, replacement_risk

    @classmethod
    def _generate_summary(cls, human: HumanState, agents: List[AgentState],
                           replacement_risk: float) -> str:
        if replacement_risk > 0.4:
            return (
                f"Frontier-level AI in your domain with declining fitness. "
                f"This plan focuses on judgment and oversight before the gap widens."
            )
        if human.token_utilization_pct < 30:
            return (
                f"You're consuming less than 30% of your AI token allocation. "
                f"The org is paying for capability you haven't unlocked yet."
            )
        if human.belief_alignment < 0.5:
            return (
                f"Belief alignment has fallen to {human.belief_alignment*100:.0f}%. "
                f"Addressing divergence is the priority — everything else follows from that."
            )
        agent_names = [a.agent_name for a in agents[:2]]
        return (
            f"Working alongside {', '.join(agent_names)}. "
            f"Plan focuses on capability gaps emerging from their current sophistication level."
        )

    @classmethod
    def _primary_theme(cls, agents: List[AgentState], human: HumanState) -> str:
        if not agents:
            return "General capability development"
        top = max(agents, key=lambda a: a.fitness_score)
        return f"Human-agent capability gap — {top.sophistication.value.title()} {top.agent_name}"
