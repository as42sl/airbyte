#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import pendulum
import pytest
from airbyte_cdk.models import AirbyteMessage, AirbyteStateMessage, AirbyteStateType, ConfiguredAirbyteCatalog, Type
from source_acceptance_test import BaseTest
from source_acceptance_test.config import IncrementalConfig
from source_acceptance_test.utils import ConnectorRunner, JsonSchemaHelper, SecretDict, filter_output, incremental_only_catalog


@pytest.fixture(name="future_state_path")
def future_state_path_fixture(inputs, base_path) -> Path:
    """Fixture with connector's future state path (relative to base_path)"""
    if getattr(inputs, "future_state_path"):
        return Path(base_path) / getattr(inputs, "future_state_path")
    pytest.skip("`future_state_path` not specified, skipping")


@pytest.fixture(name="future_state")
def future_state_fixture(future_state_path) -> Path:
    """"""
    with open(str(future_state_path), "r") as file:
        contents = file.read()
    return json.loads(contents)


@pytest.fixture(name="cursor_paths")
def cursor_paths_fixture(inputs, configured_catalog_for_incremental) -> Mapping[str, Any]:
    cursor_paths = getattr(inputs, "cursor_paths") or {}
    result = {}

    for stream in configured_catalog_for_incremental.streams:
        path = cursor_paths.get(stream.stream.name, [stream.cursor_field[-1]])
        result[stream.stream.name] = path

    return result


@pytest.fixture(name="configured_catalog_for_incremental")
def configured_catalog_for_incremental_fixture(configured_catalog) -> ConfiguredAirbyteCatalog:
    catalog = incremental_only_catalog(configured_catalog)
    for stream in catalog.streams:
        if not stream.cursor_field:
            if stream.stream.default_cursor_field:
                stream.cursor_field = stream.stream.default_cursor_field[:]
            else:
                pytest.fail(
                    f"All incremental streams should either have `cursor_field` \
                    declared in the configured_catalog or `default_cursor_field` \
                    specified in the catalog output by discover. \
                    Stream {stream.stream.name} does not have either property defined."
                )

    return catalog


def records_with_state(records, state, stream_mapping, state_cursor_paths) -> Iterable[Tuple[Any, Any]]:
    """Iterate over records and return cursor value with corresponding cursor value from state"""
    for record in records:
        stream_name = record.record.stream
        stream = stream_mapping[stream_name]
        helper = JsonSchemaHelper(schema=stream.stream.json_schema)
        cursor_field = helper.field(stream.cursor_field)
        record_value = cursor_field.parse(record=record.record.data)
        try:
            if state[stream_name] is None:
                continue

            # first attempt to parse the state value assuming the state object is namespaced on stream names
            state_value = cursor_field.parse(record=state[stream_name], path=state_cursor_paths[stream_name])
        except KeyError:
            try:
                # try second time as an absolute path in state file (i.e. bookmarks -> stream_name -> column -> value)
                state_value = cursor_field.parse(record=state, path=state_cursor_paths[stream_name])
            except KeyError:
                continue
        yield record_value, state_value, stream_name


def compare_cursor_with_threshold(record_value, state_value, threshold_days: int) -> bool:
    """
    Checks if the record's cursor value is older or equal to the state cursor value.

    If the threshold_days option is set, the values will be converted to dates so that the time-based offset can be applied.
    :raises: pendulum.parsing.exceptions.ParserError: if threshold_days is passed with non-date cursor values.
    """
    if threshold_days:

        def _parse_date_value(value) -> datetime:
            if isinstance(value, datetime):
                return value
            if isinstance(value, (int, float)):
                return pendulum.from_timestamp(value / 1000)
            return pendulum.parse(value)

        record_date_value = _parse_date_value(record_value)
        state_date_value = _parse_date_value(state_value)

        return record_date_value >= (state_date_value - pendulum.duration(days=threshold_days))

    return record_value >= state_value


def is_per_stream_state(message: AirbyteMessage) -> bool:
    return message.state and isinstance(message.state, AirbyteStateMessage) and message.state.type == AirbyteStateType.STREAM


def construct_latest_state_from_messages(messages: List[AirbyteMessage]) -> Dict[str, Mapping[str, Any]]:
    """
    Because connectors that have migrated to per-stream state only emit state messages with the new state value for a single
    stream, this helper method reconstructs the final state of all streams after going through each AirbyteMessage
    """
    latest_per_stream_by_name = dict()
    for message in messages:
        current_state = message.state
        if current_state and current_state.type == AirbyteStateType.STREAM and hasattr(current_state, "stream"):
            per_stream = current_state.stream
            latest_per_stream_by_name[per_stream.stream_descriptor.name] = per_stream.stream_state.dict() if per_stream.stream_state else {}
    return latest_per_stream_by_name


@pytest.mark.default_timeout(20 * 60)
class TestIncremental(BaseTest):
    def test_two_sequential_reads(
        self,
        inputs: IncrementalConfig,
        connector_config: SecretDict,
        configured_catalog_for_incremental: ConfiguredAirbyteCatalog,
        cursor_paths: dict[str, list[str]],
        docker_runner: ConnectorRunner,
    ):
        threshold_days = getattr(inputs, "threshold_days") or 0
        stream_mapping = {stream.stream.name: stream for stream in configured_catalog_for_incremental.streams}

        output = docker_runner.call_read(connector_config, configured_catalog_for_incremental)
        records_1 = filter_output(output, type_=Type.RECORD)
        states_1 = filter_output(output, type_=Type.STATE)

        assert states_1, "Should produce at least one state"
        assert records_1, "Should produce at least one record"

        # For legacy state format, the final state message contains the final state of all streams. For per-stream state format,
        # the complete final state of streams must be assembled by going through all prior state messages received
        if is_per_stream_state(states_1[-1]):
            latest_state = construct_latest_state_from_messages(states_1)
            state_input = [latest_per_stream for latest_per_stream in latest_state.values()]
        else:
            latest_state = states_1[-1].state.data
            state_input = states_1[-1].state.data

        for record_value, state_value, stream_name in records_with_state(records_1, latest_state, stream_mapping, cursor_paths):
            assert (
                record_value <= state_value
            ), f"First incremental sync should produce records younger or equal to cursor value from the state. Stream: {stream_name}"

        output = docker_runner.call_read_with_state(connector_config, configured_catalog_for_incremental, state=state_input)
        records_2 = filter_output(output, type_=Type.RECORD)

        for record_value, state_value, stream_name in records_with_state(records_2, latest_state, stream_mapping, cursor_paths):
            assert compare_cursor_with_threshold(
                record_value, state_value, threshold_days
            ), f"Second incremental sync should produce records older or equal to cursor value from the state. Stream: {stream_name}"

    def test_read_sequential_slices(
        self, inputs: IncrementalConfig, connector_config, configured_catalog_for_incremental, cursor_paths, docker_runner: ConnectorRunner
    ):
        """
        Incremental test that makes calls the read method without a state checkpoint. Then we partition the results by stream and
        slice checkpoints resulting in batches of messages that look like:
        <state message>
        <record message>
        ...
        <record message>

        Using these batches, we then make additional read method calls using the state message and verify the correctness of the
        messages in the response.
        """
        if inputs.skip_comprehensive_incremental_tests:
            pytest.skip("Skipping new incremental test based on acceptance-test-config.yml")
            return

        threshold_days = getattr(inputs, "threshold_days") or 0
        stream_mapping = {stream.stream.name: stream for stream in configured_catalog_for_incremental.streams}

        output = docker_runner.call_read(connector_config, configured_catalog_for_incremental)
        records_1 = filter_output(output, type_=Type.RECORD)
        states_1 = filter_output(output, type_=Type.STATE)

        assert states_1, "Should produce at least one state"
        assert records_1, "Should produce at least one record"

        # For legacy state format, the final state message contains the final state of all streams. For per-stream state format,
        # the complete final state of streams must be assembled by going through all prior state messages received
        is_per_stream = is_per_stream_state(states_1[-1])
        if is_per_stream:
            latest_state = construct_latest_state_from_messages(states_1)
        else:
            latest_state = states_1[-1].state.data

        for record_value, state_value, stream_name in records_with_state(records_1, latest_state, stream_mapping, cursor_paths):
            assert (
                record_value <= state_value
            ), f"First incremental sync should produce records younger or equal to cursor value from the state. Stream: {stream_name}"

        # Create partitions made up of one state message followed by any records that come before the next state
        filtered_messages = [message for message in output if message.type == Type.STATE or message.type == Type.RECORD]
        right_index = len(filtered_messages)
        checkpoint_messages = []
        for index, message in reversed(list(enumerate(filtered_messages))):
            if message.type == Type.STATE:
                message_group = (filtered_messages[index], filtered_messages[index + 1 : right_index])
                checkpoint_messages.insert(0, message_group)
                right_index = index

        # We sometimes have duplicate identical state messages in a stream which we can filter out to speed things up
        checkpoint_messages = [message for index, message in enumerate(checkpoint_messages) if message not in checkpoint_messages[:index]]

        # To avoid spamming APIs we only test a fraction of batches (10%) and enforce a minimum of 10 tested
        min_batches_to_test = 10
        sample_rate = len(checkpoint_messages) // min_batches_to_test
        stream_name_to_per_stream_state = dict()
        for idx, message_batch in enumerate(checkpoint_messages):
            assert len(message_batch) > 0 and message_batch[0].type == Type.STATE

            if is_per_stream:
                # Including all the latest state values from previous batches, update the combined stream state
                # with the current batch's stream state and then use it in the following read() request
                current_state = message_batch[0].state
                if current_state and current_state.type == AirbyteStateType.STREAM and hasattr(current_state, "stream"):
                    per_stream = current_state.stream
                    if per_stream.stream_state:
                        stream_name_to_per_stream_state[per_stream.stream_descriptor.name] = (
                            per_stream.stream_state.dict() if per_stream.stream_state else {}
                        )
                state_input = [latest_per_stream for latest_per_stream in stream_name_to_per_stream_state.values()]
                complete_state = stream_name_to_per_stream_state
            else:
                state_input = message_batch[0].state.data
                complete_state = message_batch[0].state.data

            if len(checkpoint_messages) >= min_batches_to_test and idx % sample_rate != 0:
                continue

            output = docker_runner.call_read_with_state(connector_config, configured_catalog_for_incremental, state=state_input)
            records = filter_output(output, type_=Type.RECORD)

            for record_value, state_value, stream_name in records_with_state(records, complete_state, stream_mapping, cursor_paths):
                assert compare_cursor_with_threshold(
                    record_value, state_value, threshold_days
                ), f"Second incremental sync should produce records older or equal to cursor value from the state. Stream: {stream_name}"

    def test_state_with_abnormally_large_values(self, connector_config, configured_catalog, future_state, docker_runner: ConnectorRunner):
        configured_catalog = incremental_only_catalog(configured_catalog)
        output = docker_runner.call_read_with_state(config=connector_config, catalog=configured_catalog, state=future_state)
        records = filter_output(output, type_=Type.RECORD)
        states = filter_output(output, type_=Type.STATE)

        assert (
            not records
        ), f"The sync should produce no records when run with the state with abnormally large values {records[0].record.stream}"
        assert states, "The sync should produce at least one STATE message"
