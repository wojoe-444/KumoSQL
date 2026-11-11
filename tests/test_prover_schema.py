from types import SimpleNamespace

import pytest

from kumosql import prover_context
from kumosql.pipeline_loading import _config_assertions
from kumosql.pipeline_types import Model, Target
from kumosql.prover_schema import DECLARED_FACTS_NOTE, from_bigquery, from_pipeline
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus

SELF_JOIN = "SELECT a.id, b.v FROM p.d.t a JOIN p.d.t b ON a.id = b.id"
PLAIN = "SELECT id, v FROM p.d.t"


def _table(name, fields, primary=None):
    data = {"schema": [{"name": n, "mode": m} for n, m in fields]}
    if primary:
        data["constraints"] = {"primaryKey": {"columns": primary}}
    return ("p", "d", name, data)


def test_assertions_are_read_from_a_config_block():
    config = """
    type: "table",
    assertions: {
      nonNull: ["id", 'v'],
      uniqueKey: ["id"],
      uniqueKeys: [["a", "b"], ["c"]],
    }
    """
    non_null, keys = _config_assertions(config)
    assert non_null == ("id", "v")
    assert keys == (("id",), ("a", "b"), ("c",))
    assert _config_assertions('type: "view"') == ((), ())


def test_bigquery_required_and_primary_key_become_facts():
    schema = from_bigquery([_table("t", [("id", "NULLABLE"), ("v", "REQUIRED")], primary=["id"])])
    facts = schema.constraints["p.d.t"]
    assert facts.not_null == {"id", "v"}
    assert facts.keys == (("id",),)
    assert schema.columns["t"] == ["id", "v"]  # every spelling is registered
    assert schema.notes == (DECLARED_FACTS_NOTE,)


def test_bare_name_shared_by_two_tables_is_dropped():
    schema = from_bigquery([
        ("p", "d1", "t", {"schema": [{"name": "a", "mode": "REQUIRED"}]}),
        ("p", "d2", "t", {"schema": [{"name": "b", "mode": "REQUIRED"}]}),
    ])
    assert "t" not in schema.columns
    assert schema.columns["d1.t"] == ["a"]


def test_tables_without_facts_add_no_note():
    schema = from_bigquery([_table("t", [("id", "NULLABLE")])])
    assert schema.notes == ()


def test_dataform_models_contribute_columns_and_assertions():
    model = Model(
        Target("p", "d", "t"), "table", "SELECT id, v FROM src", None, (), (), (), ("id",), (("id",),)
    )
    star = Model(Target("p", "d", "s"), "table", "SELECT * FROM src")
    schema = from_pipeline(SimpleNamespace(models={"p.d.t": model, "p.d.s": star}, source_schema={}))
    assert schema.columns["d.t"] == ["id", "v"]
    assert schema.constraints["t"].keys == (("id",),)
    assert "s" not in schema.columns  # a star hides the columns


def test_declared_key_lets_the_solver_prove_a_self_join_away():
    keyed = from_bigquery([_table("t", [("id", "REQUIRED"), ("v", "NULLABLE")], primary=["id"])])
    plain = from_bigquery([_table("t", [("id", "NULLABLE"), ("v", "NULLABLE")])])

    proven = prover_context.prove(SELF_JOIN, PLAIN, schema=keyed)
    assert proven.status is SmtStatus.PROVEN_EQUIVALENT
    assert DECLARED_FACTS_NOTE in proven.assumptions
    assert prover_context.prove(SELF_JOIN, PLAIN, schema=plain).status is not SmtStatus.PROVEN_EQUIVALENT


def test_settings_default_validate_and_persist(monkeypatch):
    store = {}
    monkeypatch.setattr(prover_context.state, "get_section", lambda name, default=None: store.get(name, default))
    monkeypatch.setattr(prover_context.state, "set_section", lambda name, value: store.__setitem__(name, value))
    assert prover_context.settings() == {"enabled": True, "timeout_ms": 5000, "bounded_rows": 3}
    assert prover_context.save_settings(enabled=False, timeout_ms=2000) == {"enabled": False, "timeout_ms": 2000, "bounded_rows": 3}
    assert prover_context.settings() == {"enabled": False, "timeout_ms": 2000, "bounded_rows": 3}
    assert prover_context.save_settings(bounded_rows=0)["bounded_rows"] == 0
    for bad in ({"enabled": "yes"}, {"timeout_ms": 5}, {"timeout_ms": True}):
        with pytest.raises(ValueError):
            prover_context.save_settings(**bad)


def test_bigquery_foreign_keys_reach_the_prover():
    from kumosql.prover_schema import from_bigquery

    schema = from_bigquery(
        [
            ("p", "d", "customers", {"schema": [{"name": "id", "mode": "REQUIRED"}], "constraints": {"primaryKey": {"columns": ["id"]}}}),
            (
                "p",
                "d",
                "orders",
                {
                    "schema": [{"name": "id", "mode": "REQUIRED"}, {"name": "customer_id", "mode": "REQUIRED"}],
                    "constraints": {
                        "primaryKey": {"columns": ["id"]},
                        "foreignKeys": [
                            {
                                "referencedTable": {"projectId": "p", "datasetId": "d", "tableId": "customers"},
                                "columnReferences": [{"referencingColumn": "customer_id", "referencedColumn": "id"}],
                            }
                        ],
                    },
                },
            ),
        ]
    )
    assert schema.constraints["d.orders"].foreign_keys == ((("customer_id",), "p.d.customers", ("id",)),)

    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    result = prove_equivalent_algebraic(
        "SELECT o.id FROM d.orders AS o JOIN d.customers AS c ON o.customer_id = c.id",
        "SELECT id FROM d.orders",
        schema=schema.columns,
        constraints=schema.constraints,
    )
    assert result.proven, result.reason


def test_bigquery_column_types_make_a_widening_cast_free():
    pytest.importorskip("z3")
    facts = from_bigquery([("p", "d", "t", {"schema": [{"name": "id", "type": "INTEGER"}, {"name": "amt", "type": "NUMERIC"}]})])
    plain = "SELECT id FROM d.t WHERE id > 1"
    cast = "SELECT id FROM d.t WHERE CAST(id AS NUMERIC) > 1"
    with_types = prove_equivalent_algebraic(plain, cast, schema=facts.columns, constraints=facts.constraints, types=facts.types)
    assert with_types.status is SmtStatus.PROVEN_EQUIVALENT
    narrow = "SELECT id FROM d.t WHERE CAST(amt AS NUMERIC(10, 2)) > 1"
    assert not prove_equivalent_algebraic(
        "SELECT id FROM d.t WHERE amt > 1", narrow, schema=facts.columns, types=facts.types
    ).proven
