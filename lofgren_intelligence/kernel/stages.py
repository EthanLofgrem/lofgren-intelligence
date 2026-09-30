"""The kernel loop: every objective moves through the same ordered stages.

Intent -> Plan -> Sense -> Research -> Verify -> Imagine -> Simulate -> Optimize
-> Produce -> Build -> Test -> Authorize -> Execute -> Operate -> Measure
-> Learn -> Improve -> (Research again)

Each stage belongs to the intelligence version that first implements it.
Stages from later versions are declared now so the loop is stable while the
codebase grows; the pipeline records them as "not yet available".
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    INTENT = "intent"
    PLAN = "plan"
    SENSE = "sense"
    RESEARCH = "research"
    VERIFY = "verify"
    REPORT = "report"
    IMAGINE = "imagine"
    SIMULATE = "simulate"
    OPTIMIZE = "optimize"
    PRODUCE = "produce"
    BUILD = "build"
    TEST = "test"
    AUTHORIZE = "authorize"
    EXECUTE = "execute"
    OPERATE = "operate"
    MEASURE = "measure"
    LEARN = "learn"
    IMPROVE = "improve"


KERNEL_LOOP: tuple[Stage, ...] = (
    Stage.INTENT,
    Stage.PLAN,
    Stage.SENSE,
    Stage.RESEARCH,
    Stage.VERIFY,
    Stage.REPORT,
    Stage.IMAGINE,
    Stage.SIMULATE,
    Stage.OPTIMIZE,
    Stage.PRODUCE,
    Stage.BUILD,
    Stage.TEST,
    Stage.AUTHORIZE,
    Stage.EXECUTE,
    Stage.OPERATE,
    Stage.MEASURE,
    Stage.LEARN,
    Stage.IMPROVE,
)

# Which intelligence version first delivers each stage.
STAGE_VERSION: dict[Stage, int] = {
    Stage.INTENT: 1,
    Stage.PLAN: 1,
    Stage.SENSE: 1,
    Stage.RESEARCH: 1,
    Stage.VERIFY: 1,
    Stage.REPORT: 1,
    Stage.IMAGINE: 2,
    Stage.SIMULATE: 2,
    Stage.OPTIMIZE: 2,
    Stage.PRODUCE: 3,
    Stage.BUILD: 3,
    Stage.TEST: 3,
    Stage.AUTHORIZE: 4,
    Stage.EXECUTE: 4,
    Stage.OPERATE: 4,
    Stage.MEASURE: 5,
    Stage.LEARN: 5,
    Stage.IMPROVE: 5,
}

VERSION_NAMES: dict[int, str] = {
    1: "Evidence Intelligence",
    2: "Discovery Intelligence",
    3: "Production Intelligence",
    4: "Execution Intelligence",
    5: "Outcome Intelligence",
    6: "Meta-Intelligence",
}
