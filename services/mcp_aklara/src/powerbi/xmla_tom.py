"""Power BI XMLA operations implemented with Python.NET and AMO/TOM."""

import json
import os
import sys
import urllib.parse

from ..auth.powerbi import get_powerbi_access_token


LIB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
_TOM = None
_TOM_LOAD_ATTEMPTED = False
_TOM_LOAD_ERROR = None


def _load_tom():
    """Load CoreCLR and AMO lazily and retain the first load result."""
    global _TOM, _TOM_LOAD_ATTEMPTED, _TOM_LOAD_ERROR
    if _TOM is not None:
        return _TOM
    if _TOM_LOAD_ATTEMPTED:
        raise RuntimeError(_TOM_LOAD_ERROR)

    _TOM_LOAD_ATTEMPTED = True
    try:
        if LIB_PATH not in sys.path:
            sys.path.append(LIB_PATH)
        if sys.platform != "win32":
            from pythonnet import load

            load("coreclr")

        import clr

        clr.AddReference("Microsoft.AnalysisServices.Tabular")
        import Microsoft.AnalysisServices.Tabular as tom
    except Exception as exc:
        _TOM_LOAD_ERROR = (
            "Could not load AMO/TOM. Run scripts/install_amo.py before "
            "starting the MCP service."
        )
        raise RuntimeError(_TOM_LOAD_ERROR) from exc
    _TOM = tom
    return _TOM


def tom_runtime_status() -> tuple[bool, str | None]:
    """Report whether the native TOM runtime is ready for XMLA operations."""
    try:
        _load_tom()
    except RuntimeError as exc:
        return False, str(exc)
    return True, None


def get_server_connection(workspace_name: str, access_token: str | None = None):
    tom = _load_tom()
    token = access_token or get_powerbi_access_token()
    workspace_url = urllib.parse.quote(workspace_name)
    connection = (
        "DataSource=powerbi://api.powerbi.com/v1.0/myorg/"
        f"{workspace_url};Password={token};"
    )
    server = tom.Server()
    server.Connect(connection)
    return server


def _get_model_table(server, workspace_name: str, dataset_name: str, table_name: str):
    database = server.Databases.FindByName(dataset_name)
    if not database:
        raise RuntimeError(
            f"Semantic model '{dataset_name}' was not found in '{workspace_name}'."
        )
    model = database.Model
    table = model.Tables.Find(table_name)
    if not table:
        raise RuntimeError(f"Table '{table_name}' was not found in the model.")
    return model, table


def create_measure(
    workspace_name: str,
    dataset_name: str,
    table_name: str,
    measure_name: str,
    expression: str,
    description: str = "",
    access_token: str | None = None,
) -> str:
    tom = _load_tom()
    server = get_server_connection(workspace_name, access_token)
    try:
        model, table = _get_model_table(
            server, workspace_name, dataset_name, table_name
        )
        if table.Measures.Find(measure_name):
            raise RuntimeError(
                f"Measure '{measure_name}' already exists in table '{table_name}'."
            )

        measure = tom.Measure()
        measure.Name = measure_name
        measure.Expression = expression
        if description:
            measure.Description = description
        table.Measures.Add(measure)
        model.SaveChanges()
        return f"Measure '{measure_name}' created successfully."
    finally:
        if server.Connected:
            server.Disconnect()


def update_measure(
    workspace_name: str,
    dataset_name: str,
    table_name: str,
    measure_name: str,
    expression: str,
    description: str = "",
    access_token: str | None = None,
) -> str:
    server = get_server_connection(workspace_name, access_token)
    try:
        model, table = _get_model_table(
            server, workspace_name, dataset_name, table_name
        )
        measure = table.Measures.Find(measure_name)
        if not measure:
            raise RuntimeError(
                f"Measure '{measure_name}' does not exist in table '{table_name}'."
            )

        measure.Expression = expression
        if description:
            measure.Description = description
        model.SaveChanges()
        return f"Measure '{measure_name}' updated successfully."
    finally:
        if server.Connected:
            server.Disconnect()


def get_semantic_model_schema_tom(
    workspace_name: str, dataset_name: str, access_token: str | None = None
) -> str:
    server = get_server_connection(workspace_name, access_token)
    try:
        database = server.Databases.FindByName(dataset_name)
        if not database:
            raise RuntimeError(
                f"Semantic model '{dataset_name}' was not found in '{workspace_name}'."
            )

        tables = {}
        measures = []
        relationships = []
        for table in database.Model.Tables:
            table_name = table.Name
            if table_name.startswith(("DateTable", "LocalDate")):
                continue

            columns = []
            for column in table.Columns:
                if column.Name.startswith("RowNumber-"):
                    continue
                columns.append(f"{column.Name} ({column.DataType})")
            if columns:
                tables[table_name] = columns

            for measure in table.Measures:
                item = measure.Name
                if measure.Expression:
                    item += f" := {' '.join(measure.Expression.split())}"
                if measure.Description:
                    item += f"  // {measure.Description}"
                measures.append(item)

        for relationship in database.Model.Relationships:
            if not (
                hasattr(relationship, "FromTable")
                and hasattr(relationship, "ToTable")
            ):
                continue
            from_table = relationship.FromTable.Name
            to_table = relationship.ToTable.Name
            if from_table.startswith(("DateTable", "LocalDate")) or to_table.startswith(
                ("DateTable", "LocalDate")
            ):
                continue
            relationships.append(
                f"{from_table}.{relationship.FromColumn.Name} -> "
                f"{to_table}.{relationship.ToColumn.Name}"
            )

        return json.dumps(
            {
                "Tables": tables,
                "Measures": measures,
                "Relationships": relationships,
            },
            indent=2,
            ensure_ascii=False,
        )
    finally:
        if server.Connected:
            server.Disconnect()
