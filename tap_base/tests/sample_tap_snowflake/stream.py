"""Sample tap stream test for tap-snowflake."""

from tap_base import TapStreamBase


class SampleTapSnowflakeStream(TapStreamBase):
    """Sample tap test for snowflake."""

    def __init__(self, tap_stream_id: str, schema: dict, properties: dict = None):
        """Initialize stream class."""
        pass
