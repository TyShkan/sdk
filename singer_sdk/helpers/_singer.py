from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Dict, Iterable, Tuple, Union, cast

from singer.catalog import Catalog as BaseCatalog
from singer.catalog import CatalogEntry as BaseCatalogEntry

from singer_sdk.helpers._schema import SchemaPlus

if TYPE_CHECKING:
    from typing_extensions import TypeAlias


Breadcrumb = Tuple[str, ...]

logger = logging.getLogger(__name__)


class SingerMessageType(str, enum.Enum):
    """Singer specification message types."""

    RECORD = "RECORD"
    SCHEMA = "SCHEMA"
    STATE = "STATE"
    ACTIVATE_VERSION = "ACTIVATE_VERSION"
    BATCH = "BATCH"


class SelectionMask(Dict[Breadcrumb, bool]):
    """Boolean mask for property selection in schemas and records."""

    def __missing__(self, breadcrumb: Breadcrumb) -> bool:
        """Handle missing breadcrumbs.

        - Properties default to parent value if available.
        - Root (stream) defaults to True.
        """
        if len(breadcrumb) >= 2:
            parent = breadcrumb[:-2]
            return self[parent]
        else:
            return True


@dataclass
class Metadata:
    """Base stream or property metadata."""

    class InclusionType(str, enum.Enum):
        """Catalog inclusion types."""

        AVAILABLE = "available"
        AUTOMATIC = "automatic"
        UNSUPPORTED = "unsupported"

    inclusion: InclusionType | None = None
    selected: bool | None = None
    selected_by_default: bool | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]):
        """Parse metadata dictionary."""
        return cls(
            **{
                object_field.name: value.get(object_field.name.replace("_", "-"))
                for object_field in fields(cls)
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert metadata to a JSON-encodeable dictionary."""
        result = {}

        for object_field in fields(self):
            value = getattr(self, object_field.name)
            if value is not None:
                result[object_field.name.replace("_", "-")] = value

        return result


@dataclass
class StreamMetadata(Metadata):
    """Stream metadata."""

    table_key_properties: list[str] | None = None
    forced_replication_method: str | None = None
    valid_replication_keys: list[str] | None = None
    schema_name: str | None = None


AnyMetadata: TypeAlias = Union[Metadata, StreamMetadata]


class MetadataMapping(Dict[Breadcrumb, AnyMetadata]):
    """Stream metadata mapping."""

    @classmethod
    def from_iterable(cls, iterable: Iterable[dict[str, Any]]):
        """Create a metadata mapping from an iterable of metadata dictionaries."""
        mapping: dict[Breadcrumb, AnyMetadata] = cls()
        for d in iterable:
            breadcrumb = tuple(d["breadcrumb"])
            metadata = d["metadata"]
            if breadcrumb:
                mapping[breadcrumb] = Metadata.from_dict(metadata)
            else:
                mapping[breadcrumb] = StreamMetadata.from_dict(metadata)

        return mapping

    def to_list(self) -> list[dict[str, Any]]:
        """Convert mapping to a JSON-encodable list."""
        return [
            {"breadcrumb": list(k), "metadata": v.to_dict()} for k, v in self.items()
        ]

    def __missing__(self, breadcrumb: Breadcrumb):
        """Handle missing metadata entries."""
        self[breadcrumb] = Metadata() if breadcrumb else StreamMetadata()
        return self[breadcrumb]

    @property
    def root(self):
        """Get stream (root) metadata from this mapping."""
        meta: StreamMetadata = self[()]
        return meta

    @classmethod
    def get_standard_metadata(
        cls,
        schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        key_properties: list[str] | None = None,
        valid_replication_keys: list[str] | None = None,
        replication_method: str | None = None,
    ):
        """Get default metadata for a stream."""
        mapping = cls()
        root = StreamMetadata(
            table_key_properties=key_properties,
            forced_replication_method=replication_method,
            valid_replication_keys=valid_replication_keys,
        )

        if schema:
            root.inclusion = Metadata.InclusionType.AVAILABLE

            if schema_name:
                root.schema_name = schema_name

            for field_name in schema.get("properties", {}).keys():
                if key_properties and field_name in key_properties:
                    entry = Metadata(inclusion=Metadata.InclusionType.AUTOMATIC)
                else:
                    entry = Metadata(inclusion=Metadata.InclusionType.AVAILABLE)

                mapping[("properties", field_name)] = entry

        mapping[()] = root

        return mapping

    def resolve_selection(self) -> SelectionMask:
        """Resolve selection for metadata breadcrumbs and store them in a mapping."""
        return SelectionMask(
            (breadcrumb, self._breadcrumb_is_selected(breadcrumb))
            for breadcrumb in self
        )

    def _breadcrumb_is_selected(self, breadcrumb: Breadcrumb) -> bool:
        """Determine if a property breadcrumb is selected based on existing metadata.

        An empty breadcrumb (empty tuple) indicates the stream itself. Otherwise, the
        breadcrumb is the path to a property within the stream.
        """
        if not self:
            # Default to true if no metadata to say otherwise
            return True

        md_entry = self.get(breadcrumb, Metadata())
        parent_value = None

        if len(breadcrumb) > 0:
            parent_breadcrumb = breadcrumb[:-2]
            parent_value = self._breadcrumb_is_selected(parent_breadcrumb)

        if parent_value is False:
            return parent_value

        if md_entry.inclusion == Metadata.InclusionType.UNSUPPORTED:
            if md_entry.selected is True:
                logger.debug(
                    "Property '%s' was selected but is not supported. "
                    "Ignoring selected==True input.",
                    ":".join(breadcrumb),
                )
            return False

        if md_entry.inclusion == Metadata.InclusionType.AUTOMATIC:
            if md_entry.selected is False:
                logger.debug(
                    "Property '%s' was deselected while also set "
                    "for automatic inclusion. Ignoring selected==False input.",
                    ":".join(breadcrumb),
                )
            return True

        if md_entry.selected is not None:
            return md_entry.selected

        if md_entry.selected_by_default is not None:
            return md_entry.selected_by_default

        logger.debug(
            "Selection metadata omitted for '%s'. "
            "Using parent value of selected=%s.",
            breadcrumb,
            parent_value,
        )
        return parent_value or False


@dataclass
class CatalogEntry(BaseCatalogEntry):
    """Singer catalog entry."""

    tap_stream_id: str
    metadata: MetadataMapping
    schema: SchemaPlus
    stream: str | None = None
    key_properties: list[str] | None = None
    replication_key: str | None = None
    is_view: bool | None = None
    database: str | None = None
    table: str | None = None
    row_count: int | None = None
    stream_alias: str | None = None
    replication_method: str | None = None

    @classmethod
    def from_dict(cls, stream: dict[str, Any]):
        """Create a catalog entry from a dictionary."""
        return cls(
            tap_stream_id=stream["tap_stream_id"],
            stream=stream.get("stream"),
            replication_key=stream.get("replication_key"),
            key_properties=stream.get("key_properties"),
            database=stream.get("database_name"),
            table=stream.get("table_name"),
            schema=SchemaPlus.from_dict(stream.get("schema", {})),
            is_view=stream.get("is_view"),
            stream_alias=stream.get("stream_alias"),
            metadata=MetadataMapping.from_iterable(stream.get("metadata", [])),
            replication_method=stream.get("replication_method"),
        )

    def to_dict(self):
        """Convert entry to a dictionary."""
        d = super().to_dict()
        d["metadata"] = self.metadata.to_list()
        return d


class Catalog(Dict[str, CatalogEntry], BaseCatalog):
    """Singer catalog mapping of stream entries."""

    @classmethod
    def from_dict(cls, data: dict[str, list[dict[str, Any]]]) -> Catalog:
        """Create a catalog from a dictionary."""
        instance = cls()
        for stream in data.get("streams", []):
            entry = CatalogEntry.from_dict(stream)
            instance[entry.tap_stream_id] = entry
        return instance

    def to_dict(self) -> dict[str, Any]:
        """Return a dictionary representation of the catalog.

        Returns:
            A dictionary with the defined catalog streams.
        """
        return cast(Dict[str, Any], super().to_dict())

    @property
    def streams(self) -> list[CatalogEntry]:
        """Get catalog entries."""
        return list(self.values())

    def add_stream(self, entry: CatalogEntry) -> None:
        """Add a stream entry to the catalog."""
        self[entry.tap_stream_id] = entry

    def get_stream(self, stream_id: str) -> CatalogEntry | None:
        """Retrieve a stream entry from the catalog."""
        return self.get(stream_id)
