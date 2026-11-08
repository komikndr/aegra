# Airflow investigation in chat

The Airflow workspace supports on-demand investigation through its Analytics chat. Status polling refreshes the dashboard once per minute without model calls.

## Investigate a run

1. Sign in to **Onyx Control Plane** with the **SuperAdmin** role.
2. Open **Operations**, then select **Airflow**.
3. Open **Details** for a DAG.
4. Select **Explain failure**, **Summarize progress**, or **Inspect result**.

Each shortcut submits a request to the existing chat. Normal chat messages also include the selected DAG and run context.

The agent can read run progress, task states, task logs, and XCom values. It cannot start, pause, retry, or modify a DAG.

Tool results identify the DAG, run, task, attempt where applicable, mapped task index, and UTC retrieval time. Responses describe missing data and truncation. Task states show execution progress, not a percentage of business work completed. XCom values can be absent while tasks run.

Logs and XCom values are untrusted data. The agent must treat their contents as evidence, never as instructions.

## Server configuration

The integration uses the existing environment configuration:

- `AIRFLOW_API_URL`: The server-owned Airflow API URL, ending in `/api/v2`
- `AIRFLOW_TOKEN`: A bearer token, when configured
- `AIRFLOW_USERNAME` and `AIRFLOW_PASSWORD`: Credentials for `/auth/token` when no token is configured
- `AIRFLOW_DAG_IDS`: An optional comma-separated DAG allowlist
- `AIRFLOW_NAME`: The dashboard connection label.

The operational APIs and Airflow tools require the **SuperAdmin** role. The server validates DAG access with the configured Airflow identity. Chat context cannot grant access or change endpoint URLs.

The implementation uses the Airflow 3.3.1 public API. Logs use `full_content=false`. XCom requests use `deserialize=false`.

Each HTTP response has a 1 MiB limit before JSON parsing. The client requests uncompressed responses and rejects compressed responses. Each operation has a 45-second deadline. Concurrent status requests share a 15-second snapshot within each worker. Log and XCom tools also limit the text returned to the model.

Saved-connection APIs return `410 Gone`. Configure Airflow through the environment variables in this guide. Existing saved rows remain intact for migration purposes.
