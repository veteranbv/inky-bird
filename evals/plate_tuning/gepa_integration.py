"""The one module that imports GEPA (https://github.com/gepa-ai/gepa).

GEPA drives reflective prompt evolution through an adapter with evaluate and
make_reflective_dataset, and a reflection_lm callable it asks for rewrites.
This wraps the harness's adapters in that interface. The API used here was
checked against the GEPA 0.0.7 source; the package pin allows later 0.x.
"""

from __future__ import annotations

from pathlib import Path

from inky_bird_frame.errors import MissingDependencyError

from .config import HarnessError
from .tune import EvalBatch, ReflectionLM, TuningAdapter

INSTALL_HINT = (
    "GEPA is not installed. Run the harness with its own dependencies: "
    "uv run --with-editable ./evals inky-plate-eval ..."
)


class _GepaAdapter:
    # GEPA calls a custom proposer when this is set; None selects its own
    # reflective proposer, which uses reflection_lm.
    propose_new_texts = None

    def __init__(self, adapter: TuningAdapter, batch_type: type) -> None:
        self._adapter = adapter
        self._batch_type = batch_type

    def evaluate(
        self, batch: list[object], candidate: dict[str, str], capture_traces: bool = False
    ) -> object:
        result = self._adapter.evaluate(batch, candidate, capture_traces)
        return self._batch_type(
            outputs=result.outputs, scores=result.scores, trajectories=result.trajectories
        )

    def make_reflective_dataset(
        self, candidate: dict[str, str], eval_batch: object, components_to_update: list[str]
    ) -> dict[str, list[dict[str, object]]]:
        batch = EvalBatch(
            outputs=list(getattr(eval_batch, "outputs", [])),
            scores=[float(score) for score in getattr(eval_batch, "scores", [])],
            trajectories=getattr(eval_batch, "trajectories", None),
        )
        return self._adapter.make_reflective_dataset(candidate, batch, components_to_update)


def gepa_optimizer(
    *,
    seed_candidate: dict[str, str],
    trainset: list[object],
    valset: list[object],
    adapter: TuningAdapter,
    reflection_lm: ReflectionLM,
    max_metric_calls: int,
    minibatch_size: int,
    run_dir: Path,
    seed: int,
) -> dict[str, str]:
    try:
        import gepa
    except ModuleNotFoundError as exc:
        raise MissingDependencyError(INSTALL_HINT) from exc
    run_dir.mkdir(parents=True, exist_ok=True)
    result = gepa.optimize(
        seed_candidate=seed_candidate,
        trainset=trainset,
        valset=valset,
        adapter=_GepaAdapter(adapter, gepa.EvaluationBatch),
        reflection_lm=reflection_lm,
        max_metric_calls=max_metric_calls,
        reflection_minibatch_size=minibatch_size,
        # GEPA saves its state here and resumes from it when run again.
        run_dir=str(run_dir),
        seed=seed,
    )
    best = result.best_candidate
    if not isinstance(best, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in best.items()
    ):
        raise HarnessError("GEPA returned a candidate that is not a text mapping")
    return {str(key): str(value) for key, value in best.items()}
