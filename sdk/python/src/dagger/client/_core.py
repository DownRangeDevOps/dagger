import functools
import logging
import typing
from collections import deque
from dataclasses import MISSING, asdict, dataclass, field, replace
from typing import (
    Any,
    TypeVar,
    get_type_hints,
    overload,
)

import anyio
import cattrs
import graphql
import httpx
from beartype.door import TypeHint
from cattrs.preconf.json import make_converter
from gql.client import AsyncClientSession
from gql.dsl import DSLField, DSLQuery, DSLSchema, DSLSelectable, DSLType, dsl_gql
from gql.transport.exceptions import (
    TransportClosed,
    TransportProtocolError,
    TransportQueryError,
    TransportServerError,
)

from dagger import (
    ExecuteTimeoutError,
    InvalidQueryError,
    TransportError,
)
from dagger._exceptions import _query_error_from_transport
from dagger.client._guards import (
    IDType,
    InputHint,
    InputSeqHint,
    is_id_type,
    is_id_type_sequence,
    is_id_type_subclass,
)
from dagger.client.base import Scalar, Type

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Arg(typing.NamedTuple):
    name: str  # GraphQL name
    value: Any
    default: Any = MISSING

    def as_input(self):
        if InputHint.is_bearable(self.value):
            return asdict(self.value)
        if InputSeqHint.is_bearable(self.value):
            return [asdict(v) for v in self.value]
        return self.value


@dataclass(slots=True)
class Field:
    type_name: str
    name: str
    args: dict[str, Any]
    children: dict[str, "Field"] = field(default_factory=dict)

    def to_dsl(self, schema: DSLSchema) -> DSLField:
        type_: DSLType = getattr(schema, self.type_name)
        field_ = getattr(type_, self.name)(**self.args)
        if self.children:
            field_ = field_.select(
                **{name: child.to_dsl(schema) for name, child in self.children.items()}
            )
        return field_

    def add_child(self, child: "Field") -> "Field":
        return replace(self, children={child.name: child})


@dataclass(slots=True)
class Context:
    session: AsyncClientSession
    schema: DSLSchema
    selections: deque[Field] = field(default_factory=deque)
    converter: cattrs.Converter = field(init=False)

    def __post_init__(self):
        conv = make_converter(detailed_validation=False)

        # For types that were returned from a list we need to set
        # their private attributes with a custom structuring function.

        def _needs_hook(cls: type) -> bool:
            return issubclass(cls, Type) and hasattr(cls, "__slots__")

        def _struct(d: dict[str, Any], cls: type) -> Any:
            obj = cls(self)
            hints = get_type_hints(cls)
            for slot in getattr(cls, "__slots__", ()):
                t = hints.get(slot)
                if t and slot in d:
                    setattr(obj, slot, conv.structure(d[slot], t))
            return obj

        conv.register_structure_hook_func(
            _needs_hook,
            _struct,
        )

        self.converter = conv

    def select(
        self, type_name: str, field_name: str, args: typing.Sequence[Arg]
    ) -> "Context":
        args_ = {
            arg.name: arg.as_input() for arg in args if arg.value is not arg.default
        }
        field_ = Field(type_name, field_name, args_)
        selections = self.selections.copy()
        selections.append(field_)
        return replace(self, selections=selections)

    def select_multiple(self, type_name: str, **fields: str) -> "Context":
        selections = self.selections.copy()
        parent = selections.pop()
        # When selecting multiple fields, set them as children of the last
        # selection to make `build` logic simpler.
        field_ = replace(
            parent,
            # Using kwargs for alias names. This way the returned result
            # is already formatted with the python name we expect.
            children={k: Field(type_name, v, {}) for k, v in fields.items()},
        )
        selections.append(field_)
        return replace(self, selections=selections)

    def build(self) -> DSLSelectable:
        if not self.selections:
            msg = "No field has been selected"
            raise InvalidQueryError(msg)

        def _collapse(child: Field, field_: Field):
            return field_.add_child(child)

        # This transforms the selection set into a single root Field, where
        # the `children` attribute is set to the next selection in the set,
        # and so on...
        root = functools.reduce(_collapse, reversed(self.selections))

        # `to_dsl` will cascade to all children, until the end.
        return root.to_dsl(self.schema)

    def query(self) -> graphql.DocumentNode:
        return dsl_gql(DSLQuery(self.build()))

    @overload
    async def execute(self, return_type: None = None) -> None:
        ...

    @overload
    async def execute(self, return_type: type[T]) -> T:
        ...

    async def execute(self, return_type: type[T] | None = None) -> T | None:
        await self.resolve_ids()
        query = self.query()

        try:
            result = await self.session.execute(query)
        except httpx.TimeoutException as e:
            msg = (
                "Request timed out. Try setting a higher value in 'execute_timeout' "
                "config for this `dagger.Connection()`."
            )
            raise ExecuteTimeoutError(msg) from e

        except httpx.RequestError as e:
            msg = f"Failed to make request: {e}"
            raise TransportError(msg) from e

        except TransportClosed as e:
            msg = (
                "Connection to engine has been closed. Make sure you're "
                "calling the API within a `dagger.Connection()` context."
            )
            raise TransportError(msg) from e

        except (TransportProtocolError, TransportServerError) as e:
            msg = f"Unexpected response from engine: {e}"
            raise TransportError(msg) from e

        except TransportQueryError as e:
            if error := _query_error_from_transport(e, query):
                raise error from e
            raise

        return self.get_value(result, return_type) if return_type else None

    @overload
    def get_value(self, value: None, return_type: Any) -> None:
        ...

    @overload
    def get_value(self, value: dict[str, Any], return_type: type[T]) -> T:
        ...

    def get_value(self, value: dict[str, Any] | None, return_type: type[T]) -> T | None:
        type_hint = TypeHint(return_type)

        for f in self.selections:
            if not isinstance(value, dict):
                break
            value = value[f.name]

        if value is None and not type_hint.is_bearable(value):
            msg = (
                "Required field got a null response. Check if parent fields are valid."
            )
            raise InvalidQueryError(msg)

        return self.converter.structure(value, return_type)

    async def resolve_ids(self) -> None:
        """Replace Type object instances with their ID implicitly."""

        # mutating to avoid re-fetching on forked pipeline
        async def _resolve_id(pos: int, k: str, v: IDType):
            sel = self.selections[pos]
            sel.args[k] = await v.id()

        async def _resolve_seq_id(pos: int, idx: int, k: str, v: IDType):
            sel = self.selections[pos]
            sel.args[k][idx] = await v.id()

        # resolve all ids concurrently
        async with anyio.create_task_group() as tg:
            for i, sel in enumerate(self.selections):
                for k, v in sel.args.items():
                    # check if it's a sequence of Type objects
                    if is_id_type_sequence(v):
                        # make sure it's a list, to mutate by index
                        sel.args[k] = list(v)
                        for seq_i, seq_v in enumerate(sel.args[k]):
                            if is_id_type(seq_v):
                                tg.start_soon(_resolve_seq_id, i, seq_i, k, seq_v)
                    elif is_id_type(v):
                        tg.start_soon(_resolve_id, i, k, v)


_Type = TypeVar("_Type", bound=Type)


class Root(Type):
    """Top level query object type (a.k.a. Query)."""

    @classmethod
    def _graphql_name(cls) -> str:
        return "Query"

    @classmethod
    def from_session(cls, session: AsyncClientSession):
        assert (
            session.client.schema is not None
        ), "GraphQL session has not been initialized"
        ds = DSLSchema(session.client.schema)
        ctx = Context(session, ds)
        return cls(ctx)

    @classmethod
    def from_context(cls, ctx: Context):
        return cls(replace(ctx, selections=deque()))

    def _get_object_instance(self, id_: str | Scalar, cls: type[_Type]) -> _Type:
        if not is_id_type_subclass(cls):
            msg = f"Unsupported type '{cls.__name__}'"
            raise TypeError(msg)

        if type(id_) is not cls._id_type() and not isinstance(id_, str):
            msg = f"Expected id type '{cls._id_type()}', got '{type(id_)}'"
            raise TypeError(msg)

        assert issubclass(cls, Type)
        ctx = self._select(cls._from_id_query_field(), [Arg("id", id_)])
        return cls(ctx)
