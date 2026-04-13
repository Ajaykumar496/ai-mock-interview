"""
AI Mock Interview Agent — Two-stage voice interview using LiveKit Multi-Agent.

Stage 1 — Self-Introduction:
    The interviewer greets the candidate, asks them to introduce themselves, and
    probes lightly (background, motivation, career goals). When the candidate has
    covered enough ground the agent calls `introduction_complete` (tool-based
    handoff). A time-based fallback ensures the transition fires even if the LLM
    never invokes the tool.

Stage 2 — Past Experience:
    The interviewer digs into the candidate's work history using the STAR method
    (Situation, Task, Action, Result). It asks follow-up questions and wraps up
    with a closing statement when finished.

Architecture:
    * Each stage is a separate `Agent` subclass with its own system prompt and
      tool definitions.
    * Shared state is carried in `InterviewData` (a dataclass attached to the
      session via `userdata`).
    * `on_enter` generates the first reply automatically so transitions feel
      seamless.
    * An asyncio background task watches elapsed time per stage and forces a
      handoff if the normal tool-based switch hasn't fired within the configured
      timeout.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

from livekit import api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ChatContext,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    metrics,
)
from livekit.agents.job import get_job_context
from livekit.agents.llm import function_tool
from livekit.agents.voice import MetricsCollectedEvent
from livekit.plugins import deepgram, openai, silero

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger("AI-mock-interview")

# Maximum time (seconds) before a fallback forces transition to the next stage.
INTRO_STAGE_TIMEOUT = 180        # 3 minutes for self-introduction
EXPERIENCE_STAGE_TIMEOUT = 300   # 5 minutes for past-experience

# ---------------------------------------------------------------------------
# Shared interview state
# ---------------------------------------------------------------------------

@dataclass
class InterviewData:
    """Mutable state shared across all interview agents."""

    candidate_name: Optional[str] = None
    introduction_summary: Optional[str] = None
    experience_notes: list[str] = field(default_factory=list)
    stage: str = "introduction"               # "introduction" | "experience" | "completed"
    stage_start_time: float = field(default_factory=time.time)
    handoff_triggered: bool = False            # guards against double-handoff


# ---------------------------------------------------------------------------
# Common preamble injected into every agent's system prompt
# ---------------------------------------------------------------------------

INTERVIEWER_PERSONA = (
    "Your name is Maya. You are a professional, warm, and encouraging interviewer "
    "conducting a mock job interview over a voice call. Speak naturally and "
    "conversationally — use short sentences suited for spoken dialogue. Avoid "
    "overly formal or robotic language. Listen carefully, acknowledge what the "
    "candidate says, and ask thoughtful follow-up questions. "
    "When you introduce yourself, start with a warm greeting like 'Hello there! "
    "Welcome! My name is Maya, and I'll be your interviewer today.'\n\n"
    "ABSOLUTE RULE: You have access to internal tools, but you must NEVER "
    "mention tool names, function names, or any technical details to the "
    "candidate. Never say 'experience_complete', 'introduction_complete', "
    "'note_experience', 'tool', 'function', or 'call' in your spoken responses. "
    "The candidate must never know that tools exist. Just use them silently "
    "in the background. If you are about to use a tool, do NOT announce it — "
    "just use it."
)


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 1 — Self-Introduction Agent
# ═══════════════════════════════════════════════════════════════════════════

class SelfIntroductionAgent(Agent):
    """Handles the opening of the interview: greeting + self-introduction."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                f"{INTERVIEWER_PERSONA}\n\n"
                "## Current Stage: Self-Introduction\n\n"
                "You are in the **self-introduction** stage of the interview.\n\n"
                "1. Greet the candidate warmly and introduce yourself as their "
                "   interviewer for today's mock interview session.\n"
                "2. Ask the candidate to introduce themselves — their name, "
                "   background, what they're currently doing, and what kind of "
                "   role they're looking for.\n"
                "3. Ask one or two light follow-up questions to understand their "
                "   motivation and career goals.\n"
                "4. Once you feel you have a good picture of who they are, "
                "   silently use the introduction_complete tool to move on. "
                "   Do NOT announce that you are moving on or mention any tool.\n\n"
                "Important rules:\n"
                "- Do NOT move on to asking about past work projects yet.\n"
                "- Keep this stage to about 2–3 minutes.\n"
                "- If the candidate gives very short answers, gently encourage "
                "  them to elaborate.\n"
            ),
        )

    # -- Lifecycle -----------------------------------------------------------

    async def on_enter(self) -> None:
        """Fire the first greeting automatically when this agent takes over."""
        self.session.generate_reply()

    # -- Tools ---------------------------------------------------------------

    @function_tool
    async def introduction_complete(
        self,
        context: RunContext[InterviewData],
        candidate_name: str,
        summary: str,
    ):
        """Call this when you have gathered enough information during the
        self-introduction stage and are ready to proceed to past-experience
        questions. NEVER mention this tool name to the candidate.

        Args:
            candidate_name: The candidate's name as they stated it.
            summary: A 1-2 sentence summary of the candidate's background.
        """
        ud = context.userdata

        # Guard: prevent double-handoff (tool + timeout racing)
        if ud.handoff_triggered:
            return "Transition already in progress."

        ud.handoff_triggered = True
        ud.candidate_name = candidate_name
        ud.introduction_summary = summary
        ud.stage = "experience"
        ud.stage_start_time = time.time()
        ud.handoff_triggered = False  # reset for next stage

        logger.info(
            "Stage 1 → Stage 2 (tool): candidate=%s summary=%s",
            candidate_name,
            summary,
        )

        # Return the next agent — the framework performs the handoff
        return PastExperienceAgent(
            candidate_name=candidate_name,
            intro_summary=summary,
        ), "Great, thanks for that introduction! Now let's talk about your past experience."


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 2 — Past Experience Agent
# ═══════════════════════════════════════════════════════════════════════════

class PastExperienceAgent(Agent):
    """Explores the candidate's work history using STAR-style questioning."""

    def __init__(
        self,
        candidate_name: str = "the candidate",
        intro_summary: str = "",
        *,
        chat_ctx: Optional[ChatContext] = None,
    ) -> None:
        super().__init__(
            instructions=(
                f"{INTERVIEWER_PERSONA}\n\n"
                f"## Current Stage: Past Experience\n\n"
                f"The candidate's name is **{candidate_name}**.\n"
                f"Introduction summary: {intro_summary}\n\n"
                "You are now in the **past-experience** stage.\n\n"
                "1. Ask the candidate to walk you through a specific project or "
                "   accomplishment they're proud of.\n"
                "2. Use the STAR method to probe deeper:\n"
                "   - **Situation**: What was the context?\n"
                "   - **Task**: What was your specific responsibility?\n"
                "   - **Action**: What steps did you take?\n"
                "   - **Result**: What was the outcome? Any metrics?\n"
                "3. After covering one experience thoroughly, you may ask about "
                "   a second one if time allows.\n"
                "4. When you're satisfied or the candidate wants to wrap up, "
                "   silently use the experience_complete tool. Do NOT announce "
                "   that you are ending or mention any tool name.\n\n"
                "Important rules:\n"
                "- Reference what you learned in the introduction to make the "
                "  conversation feel connected.\n"
                "- If the candidate struggles, offer encouragement and rephrase.\n"
                "- Keep this stage to about 4–5 minutes.\n"
                "- NEVER say any tool name out loud to the candidate.\n"
            ),
            chat_ctx=chat_ctx,
        )

    # -- Lifecycle -----------------------------------------------------------

    async def on_enter(self) -> None:
        """Seamlessly continue the conversation when this agent takes over."""
        self.session.generate_reply()

    # -- Tools ---------------------------------------------------------------

    @function_tool
    async def note_experience(
        self,
        context: RunContext[InterviewData],
        note: str,
    ):
        """Save a short note about a key experience or skill the candidate
        mentioned. Call this whenever the candidate shares something noteworthy.
        NEVER mention this tool to the candidate.

        Args:
            note: A concise note about the experience.
        """
        context.userdata.experience_notes.append(note)
        logger.info("Experience note saved: %s", note)
        return "Note saved. Continue the interview."

    @function_tool
    async def experience_complete(
        self,
        context: RunContext[InterviewData],
    ):
        """Call this when you have thoroughly explored the candidate's past
        experience and are ready to wrap up the interview. NEVER mention this
        tool name to the candidate."""
        ud = context.userdata

        if ud.handoff_triggered:
            return "Already wrapping up."

        ud.handoff_triggered = True
        ud.stage = "completed"

        logger.info(
            "Interview complete for %s. Notes: %s",
            ud.candidate_name,
            ud.experience_notes,
        )

        # Build notes for personalized feedback
        notes_str = ""
        if ud.experience_notes:
            notes_str = (
                "Here are specific things the candidate mentioned that you "
                "noted during the interview: "
                + "; ".join(ud.experience_notes)
                + ". You MUST reference at least 2 of these in your feedback "
                "to make it personal and specific."
            )

        # Pause to let any in-flight audio finish
        await asyncio.sleep(2)

        # Deliver the closing feedback — this is the most important part
        await self.session.generate_reply(
            instructions=(
                f"You are now delivering your final closing message to "
                f"{ud.candidate_name}. This is the most important part of the "
                "interview — the candidate is waiting for your feedback.\n\n"
                "You MUST say ALL of the following in ONE long response:\n\n"
                f"FIRST: Thank {ud.candidate_name} warmly and personally. Say "
                "something like 'Thank you so much for taking the time to speak "
                "with me today, I really enjoyed hearing about your work.'\n\n"
                "SECOND: Share 2-3 SPECIFIC strengths you noticed. Do NOT be "
                "generic. Reference ACTUAL things they said. For example:\n"
                "- 'I was really impressed by how you approached the technical "
                "challenges in your project'\n"
                "- 'Your ability to explain the impact with clear metrics shows "
                "great communication skills'\n"
                "- 'The way you handled the team dynamics showed strong "
                "leadership'\n"
                f"{notes_str}\n\n"
                "THIRD: Give 1-2 friendly improvement suggestions. Keep them "
                "constructive and positive. For example:\n"
                "- 'One small suggestion — try to include more specific numbers "
                "when describing your results'\n"
                "- 'You could strengthen your answers by focusing a bit more on "
                "your individual contributions'\n\n"
                "FOURTH: End with warm encouragement. Say something like: "
                "'Overall, you did a really great job today. I am confident "
                "you will do well in your upcoming interviews. Best of luck "
                "with everything, and take care! Goodbye!'\n\n"
                "CRITICAL RULES:\n"
                "- Say ALL four parts. Do not skip any.\n"
                "- Speak for at least 30 seconds.\n"
                "- Do NOT ask any questions.\n"
                "- Do NOT go silent.\n"
                "- Do NOT mention any tool names.\n"
                "- Be warm, genuine, and encouraging.\n"
                "- This is your ABSOLUTE FINAL message.\n"
            ),
            allow_interruptions=False,
        )

        # Wait for Maya to fully finish speaking the closing
        await asyncio.sleep(30)

        # Clean up the room
        try:
            job_ctx = get_job_context()
            await job_ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=job_ctx.room.name)
            )
        except Exception as e:
            logger.error("Error cleaning up room: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
#  Time-Based Fallback Mechanism
# ═══════════════════════════════════════════════════════════════════════════

async def _stage_timeout_watchdog(session: AgentSession[InterviewData]) -> None:
    """Background coroutine that forces a stage transition if the normal
    tool-based handoff hasn't fired within the configured timeout.

    This guarantees the interview always progresses, even if the LLM never
    decides to call the transition tool (e.g. the candidate is very talkative
    or the model gets stuck in a loop).
    """
    while True:
        await asyncio.sleep(5)  # check every 5 seconds

        ud: InterviewData = session.userdata
        elapsed = time.time() - ud.stage_start_time

        # --- Introduction timeout -------------------------------------------
        if ud.stage == "introduction" and elapsed >= INTRO_STAGE_TIMEOUT:
            if ud.handoff_triggered:
                continue

            ud.handoff_triggered = True
            logger.warning(
                "Fallback: introduction stage timed out after %.0fs — forcing handoff.",
                elapsed,
            )

            # Build a reasonable default if we never got candidate info
            name = ud.candidate_name or "there"
            summary = ud.introduction_summary or "The candidate introduced themselves."

            ud.candidate_name = name
            ud.introduction_summary = summary
            ud.stage = "experience"
            ud.stage_start_time = time.time()
            ud.handoff_triggered = False  # reset for next stage

            next_agent = PastExperienceAgent(
                candidate_name=name,
                intro_summary=summary,
            )

            # Perform the handoff through the session
            session.update_agent(next_agent)

        # --- Experience timeout ----------------------------------------------
        elif ud.stage == "experience" and elapsed >= EXPERIENCE_STAGE_TIMEOUT:
            if ud.handoff_triggered:
                continue

            ud.handoff_triggered = True
            logger.warning(
                "Fallback: experience stage timed out after %.0fs — wrapping up.",
                elapsed,
            )

            ud.stage = "completed"

            notes_str = ""
            if ud.experience_notes:
                notes_str = (
                    "Things you noted about the candidate: "
                    + "; ".join(ud.experience_notes) + ". "
                    + "Reference these in your feedback."
                )

            await asyncio.sleep(2)

            # Generate a closing message
            await session.generate_reply(
                instructions=(
                    f"Time is up. Deliver a complete closing to "
                    f"{ud.candidate_name or 'the candidate'}. "
                    "Thank them warmly, share 2 specific strengths you noticed "
                    f"({notes_str}), give 1 improvement tip, wish them luck, "
                    "and say goodbye. Do NOT ask questions. Do NOT mention tools."
                ),
                allow_interruptions=False,
            )

            await asyncio.sleep(30)

            try:
                job_ctx = get_job_context()
                await job_ctx.api.room.delete_room(
                    api.DeleteRoomRequest(room=job_ctx.room.name)
                )
            except Exception as e:
                logger.error("Error cleaning up room: %s", e)
            return  # stop the watchdog

        # --- Interview already done ------------------------------------------
        elif ud.stage == "completed":
            return


# ═══════════════════════════════════════════════════════════════════════════
#  Entrypoint & Server
# ═══════════════════════════════════════════════════════════════════════════

server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    """Pre-load the VAD model so it's ready when the first session starts."""
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    """Called once per interview session. Sets up the agent pipeline and
    launches the timeout watchdog."""

    session = AgentSession[InterviewData](
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(model="tts-1-hd", voice="nova"),
        userdata=InterviewData(),
    )

    # ── Metrics / observability ──────────────────────────────────────────
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def _log_usage():
        logger.info("Session usage: %s", usage_collector.get_summary())

    ctx.add_shutdown_callback(_log_usage)

    # ── Start the interview ──────────────────────────────────────────────
    await session.start(
        agent=SelfIntroductionAgent(),
        room=ctx.room,
    )

    # ── Launch the fallback watchdog ─────────────────────────────────────
    asyncio.create_task(_stage_timeout_watchdog(session))


# ── CLI entrypoint ───────────────────────────────────────────────────────
if __name__ == "__main__":
    cli.run_app(server)