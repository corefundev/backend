"""#574: dataset_id/dataset_version не должны теряться при row→record
маппинге — фронт строит на них прайор ETA по датасету."""
from src.storage.training_runs import PostgresTrainingRunsRegistry, to_dict


def test_row_to_record_keeps_dataset_fields():
    row = {
        "run_id": "r1", "client_id": "c1", "plan": "business",
        "data_path": "s3://x", "status": "finished",
        "dataset_id": "ds42", "dataset_version": 7,
    }
    rec = PostgresTrainingRunsRegistry._row_to_record(row)
    assert rec.dataset_id == "ds42"
    assert rec.dataset_version == 7
    d = to_dict(rec)
    assert d["dataset_id"] == "ds42" and d["dataset_version"] == 7


def test_row_without_dataset_stays_none():
    row = {"run_id": "r2", "client_id": "c1", "plan": "free",
           "data_path": "s3://y", "status": "queued"}
    rec = PostgresTrainingRunsRegistry._row_to_record(row)
    assert rec.dataset_id is None and rec.dataset_version is None
