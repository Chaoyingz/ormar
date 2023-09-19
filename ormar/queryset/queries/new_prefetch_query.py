import abc
from abc import abstractmethod
from typing import (
    Any,
    Dict,
    List,
    Sequence,
    Set,
    TYPE_CHECKING,
    Tuple,
    Type,
    Union,
    cast,
)
import logging
import sqlalchemy

import ormar  # noqa:  I100, I202
from ormar.queryset.clause import QueryClause
from ormar.queryset.queries import Query
from ormar.queryset.utils import translate_list_to_dict

if TYPE_CHECKING:  # pragma: no cover
    from ormar import Model, ForeignKeyField
    from ormar.queryset import OrderAction, FilterAction
    from ormar.models.excludable import ExcludableItems

logger = logging.getLogger(__name__)


class UniqueList(list):
    """
    Simple subclass of list that prevents the duplicates
    Cannot use set as the order is important
    """

    def append(self, item: Any) -> None:
        if item not in self:
            super().append(item)


class Node(abc.ABC):
    """
    Base Node use to build a query tree and divide job into already loaded models
    and the ones that still need to be fetched from database
    """

    def __init__(self, relation_field: "ForeignKeyField", parent: "Node") -> None:
        self.children: List["Node"] = []
        self.parent = parent
        if self.parent:
            self.parent.children.append(self)
        self.relation_field = relation_field
        self.table_prefix = ""
        self.rows: List[sqlalchemy.engine.RowProxy] = []
        self.models: List["Model"] = []
        self.use_alias: bool = False

    @property
    def target_name(self) -> str:
        """
        Return the name of the relation that is used to
        fetch excludes/includes from the excludable mixin
        as well as specifying the target to join in m2m relations

        :return: name of the relation
        :rtype: str
        """
        if (
            self.relation_field.self_reference
            and self.relation_field.self_reference_primary == self.relation_field.name
        ):
            return self.relation_field.default_source_field_name()
        else:
            return self.relation_field.default_target_field_name()

    @abstractmethod
    def extract_related_ids(
        self, column_names: Union[str, List[str]]
    ) -> List:  # pragma: no cover
        pass

    @abstractmethod
    def reload_tree(self) -> None:  # pragma: no cover
        pass

    @abstractmethod
    async def load_data(self) -> None:  # pragma: no cover
        pass

    def get_filter_for_prefetch(self) -> List["FilterAction"]:
        """
        Populates where clause with condition to return only models within the
        set of extracted ids.
        If there are no ids for relation the empty list is returned.

        :return: list of filter clauses based on original models
        :rtype: List[sqlalchemy.sql.elements.TextClause]
        """
        column_names = self.relation_field.get_model_relation_fields(
            self.parent.use_alias
        )
        ids = self.parent.extract_related_ids(column_names=column_names)

        if ids:
            return self._prepare_filter_clauses(ids=ids)
        return []

    def _prepare_filter_clauses(self, ids: List) -> List["FilterAction"]:
        """
        Gets the list of ids and construct a list of filter queries on
        extracted appropriate column names

        :param ids: list of ids that should be used to fetch data
        :type ids: List
        :return: list of filter actions to use in query
        :rtype: List["FilterAction"]
        """
        clause_target = self.relation_field.get_filter_clause_target()
        filter_column = self.relation_field.get_related_field_alias()
        qryclause = QueryClause(
            model_cls=clause_target,
            select_related=[],
            filter_clauses=[],
        )
        if isinstance(filter_column, dict):
            kwargs: Dict[str, Union[List, Set]] = dict()
            for own_name, target_name in filter_column.items():
                kwargs[f"{own_name}__in"] = set(x.get(target_name) for x in ids)
        else:
            kwargs = {f"{cast(str, filter_column)}__in": ids}
        filter_clauses, _ = qryclause.prepare_filter(_own_only=False, **kwargs)
        return filter_clauses


class AlreadyLoadedNode(Node):
    """
    Node that was already loaded in select statement
    """

    def __init__(self, relation_field: "ForeignKeyField", parent: "Node") -> None:
        super().__init__(relation_field=relation_field, parent=parent)
        self.use_alias = False
        self._extract_own_models()

    def _extract_own_models(self) -> None:
        """
        Extract own models that were already fetched and attached to root node
        """
        for model in self.parent.models:
            child_models = getattr(model, self.relation_field.name)
            if isinstance(child_models, list):
                self.models.extend(child_models)
            elif child_models:
                self.models.append(child_models)

    async def load_data(self) -> None:
        """
        Triggers a data load in the child nodes
        """
        for child in self.children:
            await child.load_data()

    def reload_tree(self) -> None:
        """
        After data was loaded we reload whole tree from the bottom
        to include freshly loaded nodes
        """
        for child in self.children:
            child.reload_tree()

    def extract_related_ids(self, column_names: Union[str, List[str]]) -> List:
        """
        Extracts the selected column(s) values from own models.
        Those values are used to construct filter clauses and populate child models.

        :param column_names: names of the column(s) that holds the relation info
        :type column_names: Union[str, List[str]]
        :return: List of extracted values of relation columns
        :rtype: List
        """
        if isinstance(column_names, list):
            return self._extract_composite_relation_keys(column_names=column_names)
        return self._extract_simple_relation_keys(column_name=column_names)

    def _extract_composite_relation_keys(self, column_names: List[str]) -> List:
        """
        Extracts composite relation keys values.

        :param column_names: names of the column(s) that holds the relation info
        :type column_names: List[str]
        :return: List of extracted values of relation columns
        :rtype: List
        """
        list_of_ids = UniqueList()
        for model in self.models:
            current_id = self._extract_current_primary_keys(
                model=model, column_names=column_names
            )
            if current_id:
                list_of_ids.append(current_id)
        return list_of_ids

    @staticmethod
    def _extract_current_primary_keys(model: "Model", column_names: List[str]) -> Dict:
        current_id = dict()
        for column in column_names:
            column = model.get_column_name_from_alias(column)
            child = getattr(model, column)
            child = child.pk if isinstance(child, ormar.Model) else child
            if child:
                current_id[model.get_column_alias(column)] = child
        return current_id

    def _extract_simple_relation_keys(self, column_name: str) -> List:
        """
        Extracts simple relation keys values.

        :param column_name: names of the column(s) that holds the relation info
        :type column_name: str
        :return: List of extracted values of relation columns
        :rtype: List
        """
        list_of_ids = UniqueList()
        for model in self.models:
            child = getattr(model, column_name)
            if isinstance(child, ormar.Model):
                list_of_ids.append(child.pk)
            elif child is not None:
                list_of_ids.append(child)
        return list_of_ids


class RootNode(AlreadyLoadedNode):
    """
    Root model Node from which both main and prefetch query originated
    """

    def __init__(self, models: List["Model"]) -> None:
        self.models = models
        self.use_alias = False
        self.children = []

    def reload_tree(self) -> None:
        for child in self.children:
            child.reload_tree()


class LoadNode(Node):
    """
    Nodes that actually need to be fetched from database in the prefetch query
    """

    def __init__(
        self,
        relation_field: "ForeignKeyField",
        excludable: "ExcludableItems",
        orders_by: List["OrderAction"],
        parent: "Node",
        source_model: Type["Model"],
    ) -> None:
        super().__init__(relation_field=relation_field, parent=parent)
        self.excludable = excludable
        self.exclude_prefix: str = ""
        self.orders_by = orders_by
        self.use_alias = True
        self.grouped_models: Dict[Any, List["Model"]] = dict()
        self.source_model = source_model

    async def load_data(self) -> None:
        """
        Ensures that at least primary key columns from current model are included in
        the query.

        Gets the filter values from the parent model and runs the query.

        Triggers a data load in child tasks.
        """
        self._update_excludable_with_related_pks()
        if self.relation_field.is_multi:
            query_target = self.relation_field.through
            select_related = [self.target_name]
        else:
            query_target = self.relation_field.to
            select_related = []

        filter_clauses = self.get_filter_for_prefetch()

        if filter_clauses:
            qry = Query(
                model_cls=query_target,
                select_related=select_related,
                filter_clauses=filter_clauses,
                exclude_clauses=[],
                offset=None,
                limit_count=None,
                excludable=self.excludable,
                order_bys=self._extract_own_order_bys(),
                limit_raw_sql=False,
            )
            expr = qry.build_select_expression()
            test = expr.compile(
                    dialect=self.source_model.Meta.database._backend._dialect,
                    compile_kwargs={"literal_binds": True},
                )
            logger.debug(
                test
            )
            self.rows = await query_target.Meta.database.fetch_all(expr)

            for child in self.children:
                await child.load_data()

    def _update_excludable_with_related_pks(self) -> None:
        """
        Makes sure that excludable is populated with own model primary keys values
        if the excludable has the exclude/include clauses
        """
        related_field_names = self.relation_field.get_related_field_name()
        alias_manager = self.relation_field.to.Meta.alias_manager
        target_model = self.relation_field.to

        original_exclude_prefix = alias_manager.resolve_relation_alias(
            from_model=self.relation_field.owner, relation_name=self.relation_field.name
        )

        relation_key = self.target_name
        source_model = self.source_model if not self.relation_field.is_multi else self.relation_field.through
        self.exclude_prefix = alias_manager.resolve_relation_alias(
            from_model=source_model, relation_name=relation_key
        )

        if original_exclude_prefix != self.exclude_prefix:
            self.excludable.copy_for_alias(
                model_cls=target_model, alias=original_exclude_prefix, target_alias=self.exclude_prefix
            )

        if self.relation_field.is_multi:
            self.table_prefix = self.exclude_prefix

        model_excludable = self.excludable.get(
            model_cls=target_model, alias=self.exclude_prefix
        )
        # includes nested pks if not included already
        for related_name in related_field_names:
            if model_excludable.include and not model_excludable.is_included(
                related_name
            ):
                model_excludable.set_values({related_name}, is_exclude=False)

    def _build_relation_string(self) -> str:
        node: Union[LoadNode, Node] = self
        relation = node.relation_field.name
        while not isinstance(node.parent, RootNode):
            relation = node.parent.relation_field.name + "__" + relation
            node = node.parent
        return relation

    def _build_relation_key(self) -> str:
        relation_key = self._build_relation_string()
        return relation_key

    def _extract_own_order_bys(self) -> List["OrderAction"]:
        """
        Extracts list of order actions related to current model.
        Since same model can happen multiple times in a tree we check not only the
        match on given model but also that path from relation tree matches the
        path in order action.

        :return: list of order actions related to current model
        :rtype: List[OrderAction]
        """
        own_order_bys = []
        own_path = self._get_full_tree_path()
        for order_by in self.orders_by:
            if (
                order_by.target_model == self.relation_field.to
                and order_by.related_str.endswith(f"{own_path}")
            ):
                order_by.is_source_model_order = True
                order_by.table_prefix = self.table_prefix
                own_order_bys.append(order_by)
        return own_order_bys

    def _get_full_tree_path(self) -> str:
        """
        Iterates the nodes to extract path from root node.

        :return: path from root node
        :rtype: str
        """
        node: Node = self
        relation_str = node.relation_field.name
        while not isinstance(node.parent, RootNode):
            node = node.parent
            relation_str = f"{node.relation_field.name}__{relation_str}"
        return relation_str

    def extract_related_ids(self, column_names: Union[str, List[str]]) -> List:
        """
        Extracts the selected column(s) values from own models.
        Those values are used to construct filter clauses and populate child models.

        :param column_names: names of the column(s) that holds the relation info
        :type column_names: Union[str, List[str]]
        :return: List of extracted values of relation columns
        :rtype: List
        """
        column_names = self._prefix_column_names_with_table_prefix(
            column_names=column_names
        )
        if len(column_names) > 1:
            return self._extract_composite_relation_keys(column_names=column_names)
        return self._extract_simple_relation_keys(column_name=column_names[0])

    def _prefix_column_names_with_table_prefix(
        self, column_names: Union[str, List[str]]
    ) -> List[str]:
        if not isinstance(column_names, list):
            column_names = [column_names]
        return [
            (f"{self.table_prefix}_" if self.table_prefix else "") + column_name
            for column_name in column_names
        ]

    def _extract_composite_relation_keys(self, column_names: List[str]) -> List:
        """
        Extracts composite relation keys values.

        :param column_names: names of the column(s) that holds the relation info
        :type column_names: List[str]
        :return: List of extracted values of relation columns
        :rtype: List
        """
        list_of_ids = UniqueList()
        for row in self.rows:
            if all(row[column_name] for column_name in column_names):
                list_of_ids.append(
                    {column_name: row[column_name] for column_name in column_names}
                )
        return list_of_ids

    def _extract_simple_relation_keys(self, column_name: str) -> List:
        """
        Extracts simple relation keys values.

        :param column_name: names of the column(s) that holds the relation info
        :type column_name: str
        :return: List of extracted values of relation columns
        :rtype: List
        """
        list_of_ids = UniqueList()
        for row in self.rows:
            if row[column_name]:
                list_of_ids.append(row[column_name])
        return list_of_ids

    def reload_tree(self) -> None:
        """
        Instantiates models from loaded database rows.
        Groups those instances by relation key for easy extract per parent.
        Triggers same for child nodes and then populates
        the parent node with own related models
        """
        if self.rows:
            self._instantiate_models()
            self._group_models_by_relation_key()
            for child in self.children:
                child.reload_tree()
            self._populate_parent_models()

    def _instantiate_models(self) -> None:
        """
        Iterates the rows and initializes instances of ormar.Models.
        Each model is instantiated only once (they can be duplicates for m2m relation
        when multiple parent models refer to same child model since the query have to
        also include the through model - hence full rows are unique, but related
        models without through models can be not unique).
        """
        fields_to_exclude = self.relation_field.to.get_names_to_exclude(
            excludable=self.excludable, alias=self.exclude_prefix
        )
        parsed_rows: Dict[Tuple, "Model"] = {}
        for row in self.rows:
            item = self.relation_field.to.extract_prefixed_table_columns(
                item={},
                row=row,
                table_prefix=self.table_prefix,
                excludable=self.excludable,
            )
            hashable_item = self._hash_item(item)
            instance = parsed_rows.setdefault(
                hashable_item,
                self.relation_field.to(**item, **{"__excluded__": fields_to_exclude}),
            )
            self.models.append(instance)

    def _hash_item(self, item: Dict) -> Tuple:
        """
        Converts model dictionary into tuple to make it hashable and allow to use it
        as a dictionary key - used to ensure unique instances of related models.

        :param item: instance dictionary
        :type item: Dict
        :return: tuple out of model dictionary
        :rtype: Tuple
        """
        result = []
        for key, value in sorted(item.items()):
            result.append(
                (key, self._hash_item(value) if isinstance(value, dict) else value)
            )
        return tuple(result)

    def _group_models_by_relation_key(self) -> None:
        """
        Groups own models by relation keys so it's easy later to extract those models
        when iterating parent models. Note that order is important as it reflects
        order by issued by the user.
        """
        relation_keys: Union[
            str, Dict[str, str], List[str]
        ] = self.relation_field.get_related_field_alias()
        if not isinstance(relation_keys, list):
            relation_keys = [relation_keys]
        for index, row in enumerate(self.rows):
            key = tuple((row[relation_key]) for relation_key in relation_keys)
            current_group = self.grouped_models.setdefault(key, [])
            current_group.append(self.models[index])

    def _populate_parent_models(self) -> None:
        """
        Populate parent node models with own child models from grouped dictionary
        """
        relation_keys = self._get_relation_keys_linking_models()
        for model in self.parent.models:
            children = self._get_own_models_related_to_parent(
                model=model, relation_keys=relation_keys
            )
            for child in children:
                setattr(model, self.relation_field.name, child)

    def _get_relation_keys_linking_models(self) -> List[Tuple[str, str]]:
        """
        Extract names and aliases of relation columns to use
        in linking between own models and parent models

        :return: tuple of name and alias of relation columns
        :rtype: List[Tuple[str, str]]
        """
        column_names = self.relation_field.get_model_relation_fields(False)
        column_aliases = self.relation_field.get_model_relation_fields(True)
        if not isinstance(column_names, list):
            column_names = [column_names]
            column_aliases = [cast(str, column_aliases)]
        return list(zip(column_names, column_aliases))

    def _get_own_models_related_to_parent(
        self, model: "Model", relation_keys: List[Tuple[str, str]]
    ) -> List["Model"]:
        """
        Extracts related column values from parent and based on this key gets the
        own grouped models.

        :param model: parent model from parent node
        :type model: Model
        :param relation_keys: name and aliases linking relations
        :type relation_keys: List[Tuple[str, str]]
        :return: list of own models to set on parent
        :rtype: List[Model]
        """
        key = {
            column_alias: getattr(model, column_name)
            for column_name, column_alias in relation_keys
        }
        for name, alias in relation_keys:
            if isinstance(key[alias], ormar.Model):
                pk_value = key[alias].pk
                key[alias] = pk_value
        final_key = tuple(key[item] for item in sorted(key.keys()))
        return self.grouped_models.get(final_key, [])


class PrefetchQuery:
    """
    Query used to fetch related models in subsequent queries.
    Each model is fetched only ones by the name of the relation.
    That means that for each prefetch_related entry next query is issued to database.
    """

    def __init__(  # noqa: CFQ002
        self,
        model_cls: Type["Model"],
        excludable: "ExcludableItems",
        prefetch_related: List,
        select_related: List,
        orders_by: List["OrderAction"],
    ) -> None:
        self.model = model_cls
        self.excludable = excludable
        self.select_dict = translate_list_to_dict(select_related, default={})
        self.prefetch_dict = translate_list_to_dict(prefetch_related, default={})
        self.orders_by = orders_by
        self.load_tasks: List[Node] = []

    async def prefetch_related(self, models: Sequence["Model"]) -> Sequence["Model"]:
        """
        Main entry point for prefetch_query.

        Receives list of already initialized parent models with all children from
        select_related already populated. Receives also list of row sql result rows
        as it's quicker to extract ids that way instead of calling each model.

        Returns list with related models already prefetched and set.

        :param models: list of already instantiated models from main query
        :type models: Sequence[Model]
        :param rows: row sql result of the main query before the prefetch
        :type rows: List[sqlalchemy.engine.result.RowProxy]
        :return: list of models with children prefetched
        :rtype: List[Model]
        """
        parent_task = RootNode(models=cast(List["Model"], models))
        self._build_load_tree(
            prefetch_dict=self.prefetch_dict,
            select_dict=self.select_dict,
            parent=parent_task,
            model=self.model,
        )
        await parent_task.load_data()
        parent_task.reload_tree()
        return parent_task.models

    def _build_load_tree(
        self,
        select_dict: Dict,
        prefetch_dict: Dict,
        parent: Node,
        model: Type["Model"],
    ) -> None:
        """
        Build a tree of already loaded nodes and nodes that need
        to be loaded through the prefetch query.

        :param select_dict: dictionary wth select query structure
        :type select_dict: Dict
        :param prefetch_dict: dictionary with prefetch query structure
        :type prefetch_dict: Dict
        :param parent: parent Node
        :type parent: Node
        :param model: currently processed model
        :type model: Model
        """
        for related in prefetch_dict.keys():
            relation_field = cast("ForeignKeyField", model.Meta.model_fields[related])
            if related in select_dict:
                task: Node = AlreadyLoadedNode(
                    relation_field=relation_field, parent=parent
                )
            else:
                task = LoadNode(
                    relation_field=relation_field,
                    excludable=self.excludable,
                    orders_by=self.orders_by,
                    parent=parent,
                    source_model=self.model,
                )
            if prefetch_dict:
                self._build_load_tree(
                    select_dict=select_dict.get(related, {}),
                    prefetch_dict=prefetch_dict.get(related, {}),
                    parent=task,
                    model=model.Meta.model_fields[related].to,
                )
