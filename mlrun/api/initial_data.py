import collections
import datetime
import os
import pathlib
import typing

import sqlalchemy.exc
import sqlalchemy.orm

import mlrun.api.db.sqldb.db
import mlrun.api.db.sqldb.models
import mlrun.api.schemas
import mlrun.artifacts
from mlrun.api.db.init_db import init_db
from mlrun.api.db.session import close_session, create_session
from mlrun.config import config
from mlrun.utils import logger

from .utils.db.alembic import AlembicUtil
from .utils.db.mysql import MySQLUtil
from .utils.db.sqlite_migration import SQLiteMigrationUtil


def init_data(from_scratch: bool = False) -> None:
    logger.info("Creating initial data")

    _perform_schema_migrations()

    _perform_database_migration(from_scratch)

    db_session = create_session()
    try:
        init_db(db_session)
        _add_initial_data(db_session)
        _perform_data_migrations(db_session)
    finally:
        close_session(db_session)
    logger.info("Initial data created")


# If the data_table version doesn't exist, we can assume the data version is 1.
# This is because data version 1 points to to a data migration which was added back in 0.6.0, and
# upgrading from a version earlier than 0.6.0 to v>=0.8.0 is not supported.
data_version_prior_to_table_addition = 1
latest_data_version = 1


def _perform_schema_migrations():
    alembic_config_file_name = "alembic.ini"
    if MySQLUtil.get_mysql_dsn_data():
        alembic_config_file_name = "alembic_mysql.ini"

    # run schema migrations on existing DB or create it with alembic
    dir_path = pathlib.Path(os.path.dirname(os.path.realpath(__file__)))
    alembic_config_path = dir_path / alembic_config_file_name

    alembic_util = AlembicUtil(alembic_config_path, _is_latest_data_version())
    alembic_util.init_alembic(config.httpdb.db.database_backup_mode == "enabled")


def _is_latest_data_version():
    db_session = create_session()
    db = mlrun.api.db.sqldb.db.SQLDB("")

    try:
        current_data_version = _resolve_current_data_version(db, db_session)
    finally:
        close_session(db_session)

    return current_data_version == latest_data_version


def _perform_database_migration(from_scratch: bool = False):
    if not from_scratch and config.httpdb.db.database_migration_mode == "enabled":
        sqlite_migration_util = SQLiteMigrationUtil()
        sqlite_migration_util.transfer()


def _perform_data_migrations(db_session: sqlalchemy.orm.Session):
    if config.httpdb.db.data_migrations_mode == "enabled":
        # FileDB is not really a thing anymore, so using SQLDB directly
        db = mlrun.api.db.sqldb.db.SQLDB("")
        current_data_version = int(db.get_current_data_version(db_session))
        if current_data_version != latest_data_version:
            logger.info(
                "Performing data migrations",
                current_data_version=current_data_version,
                latest_data_version=latest_data_version,
            )
            if current_data_version < 1:
                _perform_version_1_data_migrations(db, db_session)
            db.create_data_version(db_session, str(latest_data_version))


def _add_initial_data(db_session: sqlalchemy.orm.Session):
    # FileDB is not really a thing anymore, so using SQLDB directly
    db = mlrun.api.db.sqldb.db.SQLDB("")
    _add_default_marketplace_source_if_needed(db, db_session)
    _add_data_version(db, db_session)


def _fix_datasets_large_previews(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session,
):
    logger.info("Fixing datasets large previews")
    # get all artifacts
    artifacts = db._find_artifacts(db_session, None, "*")
    for artifact in artifacts:
        try:
            artifact_dict = artifact.struct
            if (
                artifact_dict
                and artifact_dict.get("kind") == mlrun.artifacts.DatasetArtifact.kind
            ):
                header = artifact_dict.get("header", [])
                if header and len(header) > mlrun.artifacts.dataset.max_preview_columns:
                    logger.debug(
                        "Found dataset artifact with more than allowed columns in preview fields. Fixing",
                        artifact=artifact_dict,
                    )
                    columns_to_remove = header[
                        mlrun.artifacts.dataset.max_preview_columns :
                    ]

                    # align preview
                    if artifact_dict.get("preview"):
                        new_preview = []
                        for preview_row in artifact_dict["preview"]:
                            # sanity
                            if (
                                len(preview_row)
                                < mlrun.artifacts.dataset.max_preview_columns
                            ):
                                logger.warning(
                                    "Found artifact with more than allowed columns in header definition, "
                                    "but preview data is valid. Leaving preview as is",
                                    artifact=artifact_dict,
                                )
                            new_preview.append(
                                preview_row[
                                    : mlrun.artifacts.dataset.max_preview_columns
                                ]
                            )

                        artifact_dict["preview"] = new_preview

                    # align stats
                    for column_to_remove in columns_to_remove:
                        if column_to_remove in artifact_dict.get("stats", {}):
                            del artifact_dict["stats"][column_to_remove]

                    # align schema
                    if artifact_dict.get("schema", {}).get("fields"):
                        new_schema_fields = []
                        for field in artifact_dict["schema"]["fields"]:
                            if field.get("name") not in columns_to_remove:
                                new_schema_fields.append(field)
                        artifact_dict["schema"]["fields"] = new_schema_fields

                    # lastly, align headers
                    artifact_dict["header"] = header[
                        : mlrun.artifacts.dataset.max_preview_columns
                    ]
                    logger.debug(
                        "Fixed dataset artifact preview fields. Storing",
                        artifact=artifact_dict,
                    )
                    db._store_artifact(
                        db_session,
                        artifact.key,
                        artifact_dict,
                        artifact.uid,
                        project=artifact.project,
                        tag_artifact=False,
                    )
        except Exception as exc:
            logger.warning(
                "Failed fixing dataset artifact large preview. Continuing", exc=exc,
            )


def _fix_artifact_tags_duplications(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session
):
    logger.info("Fixing artifact tags duplications")
    # get all artifacts
    artifacts = db._find_artifacts(db_session, None, "*")
    # get all artifact tags
    tags = db._query(db_session, mlrun.api.db.sqldb.models.Artifact.Tag).all()
    # artifact record id -> artifact
    artifact_record_id_map = {artifact.id: artifact for artifact in artifacts}
    tags_to_delete = []
    projects = {artifact.project for artifact in artifacts}
    for project in projects:
        artifact_keys = {
            artifact.key for artifact in artifacts if artifact.project == project
        }
        for artifact_key in artifact_keys:
            artifact_key_tags = []
            for tag in tags:
                # sanity
                if tag.obj_id not in artifact_record_id_map:
                    logger.warning("Found orphan tag, deleting", tag=tag.to_dict())
                if artifact_record_id_map[tag.obj_id].key == artifact_key:
                    artifact_key_tags.append(tag)
            tag_name_tags_map = collections.defaultdict(list)
            for tag in artifact_key_tags:
                tag_name_tags_map[tag.name].append(tag)
            for tag_name, _tags in tag_name_tags_map.items():
                if len(_tags) == 1:
                    continue
                tags_artifacts = [artifact_record_id_map[tag.obj_id] for tag in _tags]
                last_updated_artifact = _find_last_updated_artifact(tags_artifacts)
                for tag in _tags:
                    if tag.obj_id != last_updated_artifact.id:
                        tags_to_delete.append(tag)
    if tags_to_delete:
        logger.info(
            "Found duplicated artifact tags. Removing duplications",
            tags_to_delete=[
                tag_to_delete.to_dict() for tag_to_delete in tags_to_delete
            ],
            tags=[tag.to_dict() for tag in tags],
            artifacts=[artifact.to_dict() for artifact in artifacts],
        )
        for tag in tags_to_delete:
            db_session.delete(tag)
        db_session.commit()


def _find_last_updated_artifact(
    artifacts: typing.List[mlrun.api.db.sqldb.models.Artifact],
):
    # sanity
    if not artifacts:
        raise RuntimeError("No artifacts given")
    last_updated_artifact = None
    last_updated_artifact_time = datetime.datetime.min
    artifacts_with_same_update_time = []
    for artifact in artifacts:
        if artifact.updated > last_updated_artifact_time:
            last_updated_artifact = artifact
            last_updated_artifact_time = last_updated_artifact.updated
            artifacts_with_same_update_time = [last_updated_artifact]
        elif artifact.updated == last_updated_artifact_time:
            artifacts_with_same_update_time.append(artifact)
    if len(artifacts_with_same_update_time) > 1:
        logger.warning(
            "Found several artifact with same update time, heuristically choosing the first",
            artifacts=[
                artifact.to_dict() for artifact in artifacts_with_same_update_time
            ],
        )
        # we don't really need to do anything to choose the first, it's already happening because the first if is >
        # and not >=
    if not last_updated_artifact:
        logger.warning(
            "No artifact had update time, heuristically choosing the first",
            artifacts=[artifact.to_dict() for artifact in artifacts],
        )
        last_updated_artifact = artifacts[0]

    return last_updated_artifact


def _perform_version_1_data_migrations(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session
):
    _enrich_project_state(db, db_session)
    _fix_artifact_tags_duplications(db, db_session)
    _fix_datasets_large_previews(db, db_session)


def _enrich_project_state(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session
):
    logger.info("Enriching projects state")
    projects = db.list_projects(db_session)
    for project in projects.projects:
        changed = False
        if not project.spec.desired_state:
            changed = True
            project.spec.desired_state = mlrun.api.schemas.ProjectState.online
        if not project.status.state:
            changed = True
            project.status.state = project.spec.desired_state
        if changed:
            logger.debug(
                "Found project without state data. Enriching",
                name=project.metadata.name,
            )
            db.store_project(db_session, project.metadata.name, project)


def _add_default_marketplace_source_if_needed(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session
):
    try:
        hub_marketplace_source = db.get_marketplace_source(
            db_session, config.marketplace.default_source.name
        )
    except mlrun.errors.MLRunNotFoundError:
        hub_marketplace_source = None

    if not hub_marketplace_source:
        hub_source = mlrun.api.schemas.MarketplaceSource.generate_default_source()
        # hub_source will be None if the configuration has marketplace.default_source.create=False
        if hub_source:
            logger.info("Adding default marketplace source")
            # Not using db.store_marketplace_source() since it doesn't allow changing the default marketplace source.
            hub_record = db._transform_marketplace_source_schema_to_record(
                mlrun.api.schemas.IndexedMarketplaceSource(
                    index=mlrun.api.schemas.marketplace.last_source_index,
                    source=hub_source,
                )
            )
            db_session.add(hub_record)
            db_session.commit()
        else:
            logger.info("Not adding default marketplace source, per configuration")
    return


def _add_data_version(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session
):
    if db.get_current_data_version(db_session, raise_on_not_found=False) is None:
        data_version = _resolve_current_data_version(db, db_session)
        logger.info(
            "No data version, setting data version", data_version=data_version,
        )
        db.create_data_version(db_session, data_version)


def _resolve_current_data_version(
    db: mlrun.api.db.sqldb.db.SQLDB, db_session: sqlalchemy.orm.Session
):
    try:
        return int(db.get_current_data_version(db_session))
    except (sqlalchemy.exc.OperationalError, mlrun.errors.MLRunNotFoundError) as exc:
        try:
            projects = db.list_projects(db_session)
        except sqlalchemy.exc.OperationalError:
            projects = None

        # heuristic - if there are no projects it's a new DB - data version is latest
        if not projects or not projects.projects:
            logger.info(
                "No projects in DB, assuming latest data version",
                exc=exc,
                latest_data_version=latest_data_version,
            )
            return latest_data_version
        elif "no such table" in str(exc):
            logger.info(
                "Data version table does not exist, assuming prior version",
                exc=exc,
                data_version_prior_to_table_addition=data_version_prior_to_table_addition,
            )
            return data_version_prior_to_table_addition
        elif isinstance(exc, mlrun.errors.MLRunNotFoundError):
            logger.info(
                "Data version table exist without version, assuming prior version",
                exc=exc,
                data_version_prior_to_table_addition=data_version_prior_to_table_addition,
            )
            return data_version_prior_to_table_addition

        raise exc


def main() -> None:
    init_data()


if __name__ == "__main__":
    main()
