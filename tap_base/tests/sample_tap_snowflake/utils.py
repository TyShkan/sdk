#!/usr/bin/env python3
# pylint: disable=too-many-arguments,duplicate-code,too-many-locals

import copy
import datetime
import singer
import time

import singer.metrics as metrics
from singer import metadata
from singer import utils

from snowflake import connector

# LOGGER = singer.get_logger("tap_snowflake")
# LOGGER = singer.get_logger()


# TODO: Move to catalog class:


def stream_is_selected(stream):
    """Detect if stream is selected to sync."""
    md_map = metadata.to_map(stream.metadata)
    selected_md = metadata.get(md_map, (), "selected")
    return selected_md


def property_is_selected(stream, property_name):
    """Detect if field is selected to sync."""
    md_map = metadata.to_map(stream.metadata)
    return singer.should_sync_field(
        metadata.get(md_map, ("properties", property_name), "inclusion"),
        metadata.get(md_map, ("properties", property_name), "selected"),
        True,
    )


def get_is_view(catalog_entry):
    """Detect if stream is a view"""
    md_map = metadata.to_map(catalog_entry.metadata)
    return md_map.get((), {}).get("is-view")


def get_database_name(catalog_entry):
    """Get database name from catalog"""
    md_map = metadata.to_map(catalog_entry.metadata)
    return md_map.get((), {}).get("database-name")


def get_schema_name(catalog_entry):
    """Get schema name from catalog"""
    md_map = metadata.to_map(catalog_entry.metadata)
    return md_map.get((), {}).get("schema-name")


def get_key_properties(catalog_entry):
    """Get key properties from catalog"""
    catalog_metadata = metadata.to_map(catalog_entry.metadata)
    stream_metadata = catalog_metadata.get((), {})
    is_view = get_is_view(catalog_entry)
    if is_view:
        key_properties = stream_metadata.get("view-key-properties", [])
    else:
        key_properties = stream_metadata.get("table-key-properties", [])
    return key_properties


def generate_select_sql(catalog_entry, columns):
    """Generate SQL to extract data froom snowflake"""
    database_name = get_database_name(catalog_entry)
    schema_name = get_schema_name(catalog_entry)
    escaped_db = escape(database_name)
    escaped_schema = escape(schema_name)
    escaped_table = escape(catalog_entry.table)
    escaped_columns = []
    for col_name in columns:
        escaped_col = escape(col_name)
        # fetch the column type format from the json schema alreay built
        property_format = catalog_entry.schema.properties[col_name].format
        # if the column format is binary, fetch the hexified value
        if property_format == "binary":
            escaped_columns.append(f"hex_encode({escaped_col}) as {escaped_col}")
        else:
            escaped_columns.append(escaped_col)
    select_sql = f'SELECT {",".join(escaped_columns)} FROM {escaped_db}.{escaped_schema}.{escaped_table}'
    # escape percent signs
    select_sql = select_sql.replace("%", "%%")
    return select_sql


def whitelist_bookmark_keys(bookmark_key_set, tap_stream_id, state):
    """..."""
    for bookmark_key in [
        non_whitelisted_bookmark_key
        for non_whitelisted_bookmark_key in state.get("bookmarks", {})
        .get(tap_stream_id, {})
        .keys()
        if non_whitelisted_bookmark_key not in bookmark_key_set
    ]:
        singer.clear_bookmark(state, tap_stream_id, bookmark_key)


def sync_query(
    cursor: connector.cursor.SnowflakeCursor,
    catalog_entry,
    state,
    select_sql: str,
    columns,
    stream_version,
    params,
):
    """..."""
    replication_key = singer.get_bookmark(
        state, catalog_entry.tap_stream_id, "replication_key"
    )
    time_extracted = utils.now()
    LOGGER.info("Running %s", select_sql)
    cursor.execute(select_sql, params)
    row = cursor.fetchone()
    rows_saved = 0
    database_name = get_database_name(catalog_entry)
    with metrics.record_counter(None) as counter:
        counter.tags["database"] = database_name
        counter.tags["table"] = catalog_entry.table
        while row:
            counter.increment()
            rows_saved += 1
            record_message = row_to_singer_record(
                catalog_entry, stream_version, row, columns, time_extracted
            )
            singer.write_message(record_message)
            md_map = metadata.to_map(catalog_entry.metadata)
            stream_metadata = md_map.get((), {})
            replication_method = stream_metadata.get("replication-method")
            if replication_method == "FULL_TABLE":
                key_properties = get_key_properties(catalog_entry)
                max_pk_values = singer.get_bookmark(
                    state, catalog_entry.tap_stream_id, "max_pk_values"
                )
                if max_pk_values:
                    last_pk_fetched = {
                        k: v
                        for k, v in record_message.record.items()
                        if k in key_properties
                    }
                    state = singer.write_bookmark(
                        state,
                        catalog_entry.tap_stream_id,
                        "last_pk_fetched",
                        last_pk_fetched,
                    )
            elif replication_method == "INCREMENTAL":
                if replication_key is not None:
                    state = singer.write_bookmark(
                        state,
                        catalog_entry.tap_stream_id,
                        "replication_key",
                        replication_key,
                    )
                    state = singer.write_bookmark(
                        state,
                        catalog_entry.tap_stream_id,
                        "replication_key_value",
                        record_message.record[replication_key],
                    )
            if rows_saved % 1000 == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
            row = cursor.fetchone()
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
