from typing import Any

import structlog

from src.agent_server.settings import settings

from .base import ObservabilityProvider

logger = structlog.getLogger(__name__)


class MLflowProvider(ObservabilityProvider):
    """MLflow observability provider."""

    def get_callbacks(self) -> list[Any]:
        """return empty callbacks[] for compat reason, mlflow.autolog does not need wrapping."""
        if self.is_enabled():
            try:
                import mlflow
                mlflow.langchain.autolog()

                uri = settings.mlflow.MLFLOW_TRACKING_URI
                experiment = settings.mlflow.MLFLOW_EXPERIMENT

                mlflow.set_tracking_uri(uri)
                mlflow.set_experiment(experiment)
                logger.info("Mlflow tracing enabled, autolog created.")
            except ImportError:
                logger.warning(
                    "MLFLOW_LOGGING is true, but 'mlflow' is not installed. "
                    "Please run 'pip install mlflow' to enable tracing."
                )
            except Exception as e:
                logger.error(f"Failed to initialize MLflow: {e}")

        return []

    def get_metadata(
        self, run_id: str, thread_id: str, user_identity: str | None = None
    ) -> dict[str, Any]:
        """Return MLflow metadata."""
        metadata: dict[str, Any] = {
            "mlflow_session_id": thread_id,
        }

        if user_identity:
            metadata["mlflow_user_id"] = user_identity
            metadata["mlflow_tags"] = [
                "aegra_run",
                f"run:{run_id}",
                f"thread:{thread_id}",
                f"user:{user_identity}",
            ]
        else:
            metadata["mlflow_tags"] = [
                "aegra_run",
                f"run:{run_id}",
                f"thread:{thread_id}",
            ]

        return metadata

    def is_enabled(self) -> bool:
        """Check if Langfuse is enabled."""
        return settings.mlflow.MLFLOW_LOGGING


# Create and register the MLflow provider
_mlflow_provider = MLflowProvider()


def get_tracing_callbacks() -> list[Any]:
    """
    Backward compatibility function - delegates to the new observability system.
    """
    from .base import get_observability_manager

    # Register the mlflow provider unconditionally; registration should be idempotent
    manager = get_observability_manager()
    manager.register_provider(_mlflow_provider)

    return manager.get_all_callbacks()
