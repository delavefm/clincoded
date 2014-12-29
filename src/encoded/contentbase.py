# See http://docs.pylonsproject.org/projects/pyramid/en/latest/narr/resources.html
import logging
import sys
import venusian
from abc import ABCMeta
from collections import Mapping
from copy import deepcopy
from future.utils import (
    raise_with_traceback,
    with_metaclass,
    itervalues,
)
from itertools import islice
from past.builtins import basestring
from posixpath import join
from pyramid.events import (
    ContextFound,
    subscriber,
)
from pyramid.exceptions import PredicateMismatch
from pyramid.httpexceptions import (
    HTTPConflict,
    HTTPForbidden,
    HTTPInternalServerError,
    HTTPNotFound,
    HTTPPreconditionFailed,
)
from pyramid.interfaces import (
    PHASE2_CONFIG,
)
from pyramid.location import lineage
from pyramid.security import (
    ALL_PERMISSIONS,
    Allow,
    Authenticated,
    Deny,
    Everyone,
    has_permission,
    principals_allowed_by_permission,
)
from pyramid.settings import asbool
from pyramid.traversal import (
    find_root,
    resource_path,
)
from pyramid.view import (
    render_view_to_response,
    view_config,
)
from sqlalchemy import (
    bindparam,
    orm,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import (
    FlushError,
    NoResultFound,
)
from urllib.parse import urlencode
from uuid import (
    UUID,
    uuid4,
)
from .cache import ManagerLRUCache
from .objtemplate import ObjectTemplate
from .precompiled_query import precompiled_query_builder
from .embedding import (
    embed,
    expand_path,
)
from .schema_formats import is_accession
from .schema_utils import validate_request
from .storage import (
    DBSession,
    CurrentPropertySheet,
    Resource,
    Key,
    Link,
)
from collections import (
    OrderedDict,
    defaultdict,
)
from .validation import ValidationFailure

PHASE1_5_CONFIG = -15


LOCATION_ROOT = __name__ + ':location_root'
_marker = object()

logger = logging.getLogger(__name__)


def includeme(config):
    config.scan(__name__)
    config.set_root_factory(root_factory)


def root_factory(request):
    return request.registry[LOCATION_ROOT]


# No-validation validators

def no_validate_item_content_post(context, request):
    data = request.json
    request.validated.update(data)


def no_validate_item_content_put(context, request):
    data = request.json
    if 'uuid' in data:
        if UUID(data['uuid']) != context.uuid:
            msg = 'uuid may not be changed'
            raise ValidationFailure('body', ['uuid'], msg)
    request.validated.update(data)


def no_validate_item_content_patch(context, request):
    data = context.properties.copy()
    data.update(request.json)
    if 'uuid' in data:
        if UUID(data['uuid']) != context.uuid:
            msg = 'uuid may not be changed'
            raise ValidationFailure('body', ['uuid'], msg)
    request.validated.update(data)


# Schema checking validators

def validate_item_content_post(context, request):
    data = request.json
    schema = context.schema
    if schema is None:
        request.validated.update(data)
        return
    validate_request(schema, request, data)


def validate_item_content_put(context, request):
    data = request.json
    schema = context.schema
    if schema is None:
        if 'uuid' in data:
            if UUID(data['uuid']) != context.uuid:
                msg = 'uuid may not be changed'
                raise ValidationFailure('body', ['uuid'], msg)
        request.validated.update(data)
        return
    current = context.upgrade_properties(finalize=False).copy()
    current['uuid'] = str(context.uuid)
    validate_request(schema, request, data, current)


def validate_item_content_patch(context, request):
    data = context.upgrade_properties(finalize=False).copy()
    if 'schema_version' in data:
        del data['schema_version']
    data.update(request.json)
    schema = context.schema
    if schema is None:
        if 'uuid' in data:
            if UUID(data['uuid']) != context.uuid:
                msg = 'uuid may not be changed'
                raise ValidationFailure('body', ['uuid'], msg)
        request.validated.update(data)
        return
    current = context.upgrade_properties(finalize=False).copy()
    current['uuid'] = str(context.uuid)
    validate_request(schema, request, data, current)


def permission_checker(context, request):
    def checker(permission):
        return request.has_permission(permission, context)
    return checker


def acl_from_settings(settings):
    acl = []
    for k, v in settings.items():
        if k.startswith('allow.'):
            action = Allow
            permission = k[len('allow.'):]
            principals = v.split()
        elif k.startswith('deny.'):
            action = Deny
            permission = k[len('deny.'):]
            principals = v.split()
        else:
            continue
        if permission == 'ALL_PERMISSIONS':
            permission = ALL_PERMISSIONS
        for principal in principals:
            if principal == 'Authenticated':
                principal = Authenticated
            elif principal == 'Everyone':
                principal = Everyone
            acl.append((action, principal, permission))
    return acl


def uncamel(string):
    """ CamelCase -> camel_case
    """
    out = ''
    before = ''
    for char in string:
        if char.isupper() and before.isalnum() and not before.isupper():
            out += '_'
        out += char.lower()
        before = char
    return out


def location_root(factory):
    """ Set the location root
    """

    def set_root(config, factory):
        acl = acl_from_settings(config.registry.settings)
        root = factory(config.registry, acl)
        config.registry[LOCATION_ROOT] = root

    def callback(scanner, factory_name, factory):
        scanner.config.action(('location_root',), set_root,
                              args=(scanner.config, factory),
                              order=PHASE1_5_CONFIG)
    venusian.attach(factory, callback, category='pyramid')

    return factory


def location(name, **kw):
    """ Attach a collection at the location ``name``.

    Use as a decorator on Collection subclasses.
    """

    def set_location(config, name, factory, **kw):
        root = config.registry[LOCATION_ROOT]
        collection = factory(root, name, **kw)
        root[name] = collection

    def decorate(factory):
        if issubclass(factory, Item):
            kw['Item'] = factory
            collection_class = factory.Collection
        else:
            collection_class = factory

        def callback(scanner, factory_name, factory):
            scanner.config.action(('location', name), set_location,
                                  args=(scanner.config, name, collection_class),
                                  kw=kw,
                                  order=PHASE2_CONFIG)
        venusian.attach(factory, callback, category='pyramid')
        return factory

    return decorate


def _get_by_uuid_instance_map(uuid):
    # Internals from sqlalchemy/orm/query.py:Query.get
    session = DBSession()
    mapper = orm.class_mapper(Resource)
    ident = [uuid]
    key = mapper.identity_key_from_primary_key(ident)
    return orm.loading.get_from_identity(
        session, key, orm.attributes.PASSIVE_OFF)


@precompiled_query_builder(DBSession)
def _get_by_uuid_query():
    session = DBSession()
    return session.query(Resource).filter(Resource.rid == bindparam('rid'))


@precompiled_query_builder(DBSession)
def _get_by_unique_key_query():
    session = DBSession()
    return session.query(Key).options(
        orm.joinedload_all(
            Key.resource,
            Resource.data,
            CurrentPropertySheet.propsheet,
            innerjoin=True,
        ),
    ).filter(Key.name == bindparam('name'), Key.value == bindparam('value'))


class Root(object):
    __name__ = ''
    __parent__ = None
    schema = None
    builtin_acl = [
        (Allow, 'remoteuser.INDEXER', ('view', 'list', 'traverse', 'index')),
        (Allow, 'remoteuser.EMBED', ('view', 'traverse', 'expand', 'audit')),
    ]

    def __init__(self, registry, acl=None):
        self.registry = registry
        if acl is None:
            acl = []
        self.__acl__ = acl + self.builtin_acl
        self.collections = {}
        self.by_item_type = {}
        self.item_cache = ManagerLRUCache('encoded_item_cache', 1000)
        self.unique_key_cache = ManagerLRUCache('encoded_key_cache', 1000)
        self.type_back_rev = {}

    def __getitem__(self, name):
        try:
            resource = self.get(name)
        except KeyError:
            # Just in case we get an unexpected KeyError
            # FIXME: exception logging.
            raise HTTPInternalServerError('Traversal raised KeyError')
        if resource is None:
            raise KeyError(name)
        return resource

    def __contains__(self, name):
        return self.get(name, None) is not None

    def get(self, name, default=None):
        resource = self.collections.get(name, None)
        if resource is not None:
            return resource
        resource = self.by_item_type.get(name, None)
        if resource is not None:
            return resource
        resource = self.get_by_uuid(name, None)
        if resource is not None:
            return resource
        resource = self.get_by_unique_key('page:location', name)
        if resource is not None:
            return resource
        if is_accession(name):
            resource = self.get_by_unique_key('accession', name)
            if resource is not None:
                return resource
        if ':' in name:
            resource = self.get_by_unique_key('alias', name)
            if resource is not None:
                return resource
        return default

    def __setitem__(self, name, value):
        self.collections[name] = value
        self.by_item_type[value.item_type] = value

        # Calculate the reverse rev map
        for prop_name, spec in value.Item.merged_rev.items():
            item_type, rel = spec
            back = self.type_back_rev.setdefault(item_type, {}).setdefault(rel, set())
            back.add((value.item_type, prop_name))

    def get_by_uuid(self, uuid, default=None):
        if isinstance(uuid, basestring):
            try:
                uuid = UUID(uuid)
            except ValueError:
                return default
        elif not isinstance(uuid, UUID):
            raise TypeError(uuid)

        cached = self.item_cache.get(uuid)
        if cached is not None:
            return cached

        model = _get_by_uuid_instance_map(uuid)

        if model is None:
            try:
                model = _get_by_uuid_query().params(rid=uuid).one()
            except NoResultFound:
                return default

        collection = self.by_item_type[model.item_type]
        item = collection.Item(collection, model)
        self.item_cache[uuid] = item
        return item

    def get_by_unique_key(self, unique_key, name, default=None):
        pkey = (unique_key, name)

        cached = self.unique_key_cache.get(pkey)
        if cached is not None:
            return self.get_by_uuid(cached)

        try:
            key = _get_by_unique_key_query().params(name=unique_key, value=name).one()
        except NoResultFound:
            return default

        model = key.resource

        uuid = model.rid
        self.unique_key_cache[pkey] = uuid
        cached = self.item_cache.get(uuid)
        if cached is not None:
            return cached

        collection = self.by_item_type[model.item_type]
        item = collection.Item(collection, model)
        self.item_cache[uuid] = item
        return item

    def __json__(self, request=None):
        return self.properties.copy()


class MergedDictsMeta(type):
    """ Merge dicts on the subclass with its bases
    """
    def __init__(self, name, bases, attrs):
        super(MergedDictsMeta, self).__init__(name, bases, attrs)
        for attr in self.__merged_dicts__:
            merged = {}
            setattr(self, 'merged_%s' % attr, merged)

            for cls in reversed(self.mro()):
                value = vars(cls).get(attr, None)
                if value is not None:
                    merged.update(value)


class MergedKeysMeta(MergedDictsMeta):
    """ Merge the keys from the subclass with its bases
    """
    def __init__(self, name, bases, attrs):
        super(MergedKeysMeta, self).__init__(name, bases, attrs)

        self.merged_keys = []
        for cls in reversed(self.mro()):
            for key in vars(cls).get('keys', []):
                if isinstance(key, basestring):
                    key = {'name': '{item_type}:' + key,
                           'value': '{%s}' % key, '$templated': True}
                self.merged_keys.append(key)

        self.embedded_paths = sorted({tuple(path.split('.')) for path in self.embedded})
        if self.audit_inherit is None:
            self.audit_inherit_paths = self.embedded_paths
        else:
            self.audit_inherit_paths = sorted(
                {tuple(path.split('.')) for path in self.audit_inherit})


class Item(with_metaclass(MergedKeysMeta, object)):
    __merged_dicts__ = [
        'template',
        'rev',
        'namespace_from_path',
    ]
    base_types = ['item']
    keys = []
    name_key = None
    rev = None
    embedded = ()
    audit_inherit = None
    namespace_from_path = {}
    template = {
        '@id': {'$value': '{item_uri}', '$templated': True},
        # 'collection': '{collection_uri}',
        '@type': [
            {'$value': '{item_type}', '$templated': True},
            {'$value': '{base}', '$repeat': 'base base_types', '$templated': True},
        ],
        'uuid': {'$value': '{uuid}', '$templated': True},
    }
    actions = []

    def __init__(self, collection, model):
        self.collection = collection
        self.model = model

    def __repr__(self):
        return '<%s at %s>' % (type(self).__name__, resource_path(self))

    @property
    def __parent__(self):
        return self.collection

    @property
    def __name__(self):
        if self.name_key is None:
            return self.uuid
        return self.properties.get(self.name_key, None) or self.uuid

    @property
    def item_type(self):
        return self.model.item_type

    @property
    def schema(self):
        return self.collection.schema

    @property
    def schema_version(self):
        return self.collection.schema_version

    @property
    def properties(self):
        return self.model['']

    @property
    def propsheets(self):
        return self.model

    @property
    def uuid(self):
        return self.model.rid

    @property
    def schema_links(self):
        return self.collection.schema_links

    @property
    def merged_back_rev(self):
        root = find_root(self)
        merged = {}
        types = [self.item_type] + self.base_types
        for item_type in reversed(types):
            merged.update(root.type_back_rev.get(item_type, ()))
        return merged

    def rev_links(self):
        root = find_root(self)
        links = {}
        for name, spec in self.merged_rev.items():
            links[name] = value = []
            for link in self.model.revs:
                if (link.source.item_type, link.rel) == spec:
                    item = root.get_by_uuid(link.source_rid)
                    value.append(item)
        return links

    def upgrade_properties(self, finalize=True):
        properties = self.properties.copy()
        current_version = properties.get('schema_version', '')
        target_version = self.schema_version
        if target_version is not None and current_version != target_version:
            root = find_root(self)
            migrator = root.registry['migrator']
            try:
                properties = migrator.upgrade(
                    self.item_type, properties, current_version, target_version,
                    finalize=finalize, context=self, registry=root.registry)
            except RuntimeError:
                raise
            except Exception:
                logger.warning(
                    'Unable to upgrade %s from %r to %r',
                    resource_path(self.__parent__, self.uuid),
                    current_version, target_version, exc_info=True)
        return properties

    def __json__(self, request):
        # Record embedding objects
        request._embedded_uuids.add(str(self.uuid))
        return self.upgrade_properties()

    def __resource_url__(self, request, info):
        # Record linking objects
        request._linked_uuids.add(str(self.uuid))
        return None

    def template_namespace(self, properties, request=None):
        ns = properties.copy()
        ns['properties'] = properties
        ns['item_type'] = self.item_type
        ns['base_types'] = self.base_types
        ns['uuid'] = self.uuid
        ns['root'] = root = find_root(self)
        ns['context'] = self
        ns['registry'] = root.registry
        ns['collection_uri'] = resource_path(self.__parent__, '')
        ns['item_uri'] = resource_path(self, '')

        # When called by update_keys() there is no request.
        if request is not None:
            ns['request'] = request
            ns['permission'] = permission_checker(self, request)
            # Use request.resource_path so that linked uuid is recorded
            ns['item_uri'] = request.resource_path(self)
            for name, value in self.rev_links().items():
                ns[name] = [request.resource_path(item) for item in value]
        else:
            ns['item_uri'] = resource_path(self, '')
            for name, value in self.rev_links().items():
                ns[name] = [resource_path(item, '') for item in value]

        if self.merged_namespace_from_path:
            root = find_root(self)
            for name, paths in self.merged_namespace_from_path.items():
                # Treat a list of paths as a search path for the value
                if isinstance(paths, basestring):
                    paths = [paths]
                for path in paths:
                    path = path.split('.')
                    last = path[-1]
                    obj = self
                    obj_props = obj.upgrade_properties(finalize=False)
                    for n in path[:-1]:
                        if n not in obj_props:
                            break
                        obj = root.get_by_uuid(obj_props[n])
                        obj_props = obj.upgrade_properties(finalize=False)
                    else:
                        if last in obj_props:
                            ns[name] = deepcopy(obj_props[last])
                            break

        return ns

    @classmethod
    def expand_page(cls, request, properties):
        return properties

    @classmethod
    def create(cls, parent, uuid, properties, sheets=None):
        item_type = parent.item_type
        session = DBSession()
        sp = session.begin_nested()
        resource = Resource(item_type, rid=uuid)
        cls.update_properties(resource, properties, sheets)
        session.add(resource)
        self = cls(parent, resource)
        keys_add, keys_remove = self.update_keys()
        self.update_rels()
        try:
            sp.commit()
        except (IntegrityError, FlushError):
            sp.rollback()
        else:
            return self

        # Try again more carefully
        cls.update_properties(resource, properties, sheets)
        session.add(resource)
        self = cls(parent, resource)
        try:
            session.flush()
        except (IntegrityError, FlushError):
            msg = 'UUID conflict'
            raise HTTPConflict(msg)
        conflicts = self.check_duplicate_keys(keys_add)
        self.update_properties(properties, sheets)
        assert conflicts
        msg = 'Keys conflict: %r' % conflicts
        raise HTTPConflict(msg)

    @classmethod
    def update_properties(cls, model, properties, sheets=None):
        if properties is not None:
            if 'uuid' in properties:
                properties = properties.copy()
                del properties['uuid']
            model[''] = properties
        if sheets is not None:
            for key, value in sheets.items():
                model[key] = value

    def update(self, properties, sheets=None):
        session = DBSession()
        sp = session.begin_nested()
        self.update_properties(self.model, properties, sheets)
        keys_add, keys_remove = self.update_keys()
        self.update_rels()
        try:
            sp.commit()
        except (IntegrityError, FlushError):
            sp.rollback()
        else:
            return

        # Try again more carefully
        self.update_properties(self.model, properties, sheets)
        try:
            session.flush()
        except (IntegrityError, FlushError):
            msg = 'Properties conflict'
            raise HTTPConflict(msg)
        conflicts = self.check_duplicate_keys(keys_add)
        self.update_properties(self.model, properties, sheets)
        assert conflicts
        msg = 'Keys conflict: %r' % conflicts
        raise HTTPConflict(msg)

    def update_keys(self):
        session = DBSession()
        ns = self.template_namespace(self.properties)
        compiled = ObjectTemplate(self.merged_keys)
        _keys = [(key['name'], key['value']) for key in compiled(ns)]
        keys = set(_keys)

        if len(keys) != len(_keys):
            msg = "Duplicate keys: %r" % _keys
            raise ValidationFailure('body', [], msg)

        existing = {
            (key.name, key.value)
            for key in self.model.unique_keys
        }

        to_remove = existing - keys
        to_add = keys - existing

        for pk in to_remove:
            key = session.query(Key).get(pk)
            session.delete(key)

        for name, value in to_add:
            key = Key(rid=self.uuid, name=name, value=value)
            session.add(key)

        return to_add, to_remove

    def check_duplicate_keys(self, keys):
        session = DBSession()
        return [pk for pk in keys if session.query(Key).get(pk) is not None]

    def update_rels(self):
        if self.schema is None:
            return
        session = DBSession()
        properties = self.properties
        source = self.uuid
        _rels = []

        for name in self.schema_links:
            targets = properties.get(name, [])
            if not isinstance(targets, list):
                targets = [targets] if targets else []
            for target in targets:
                _rels.append((name, UUID(target)))

        rels = set(_rels)
        if len(rels) != len(_rels):
            msg = "Duplicate links: %r" % _rels
            raise ValidationFailure('body', [], msg)

        existing = {
            (link.rel, link.target_rid)
            for link in self.model.rels
        }

        to_remove = existing - rels
        to_add = rels - existing

        for rel, target in to_remove:
            link = session.query(Link).get((source, rel, target))
            session.delete(link)

        for rel, target in to_add:
            link = Link(source_rid=source, rel=rel, target_rid=target)
            session.add(link)

        return to_add, to_remove


class CustomItemMeta(MergedDictsMeta, ABCMeta):
    """ Give each collection its own Item class to enable
        specific view registration.
    """
    def __init__(self, name, bases, attrs):
        super(CustomItemMeta, self).__init__(name, bases, attrs)

        # XXX Remove this, too magical.
        if self.item_type is None and 'item_type' not in attrs:
            self.item_type = uncamel(self.__name__)

        NAMES_TO_TRANSFER = [
            'template',
            'embedded',
            'keys',
            'rev',
            'name_key',
            'namespace_from_path',
            'actions',
        ]

        if 'Item' in attrs:
            for name in NAMES_TO_TRANSFER:
                assert 'item_' + name not in attrs
            return
        item_bases = tuple(base.Item for base in bases
                           if issubclass(base, Collection))
        item_attrs = {'__module__': self.__module__}
        for name in NAMES_TO_TRANSFER:
            if 'item_' + name in attrs:
                item_attrs[name] = attrs['item_' + name]
        self.Item = type('Item', item_bases, item_attrs)


class Collection(with_metaclass(CustomItemMeta, Mapping)):
    __merged_dicts__ = [
        'template',
    ]
    Item = Item
    schema = None
    schema_links = ()
    schema_version = None
    properties = OrderedDict()
    item_type = None
    unique_key = None
    columns = {}
    template = {
        '@id': {'$value': '{collection_uri}', '$templated': True},
        '@type': [
            {'$value': '{item_type}_collection', '$templated': True},
            'collection',
        ],
    }

    def __init__(self, parent, name, Item=None, item_type=None, properties=None, acl=None, unique_key=None):
        self.__name__ = name
        self.__parent__ = parent
        if Item is not None:
            self.Item = Item
            self.item_type = Item.item_type
            self.schema = Item.schema
        if properties is not None:
            self.properties = properties
        if acl is not None:
            self.__acl__ = acl
        if unique_key is not None:
            self.unique_key = unique_key

        self.column_paths = set()
        if self.schema is not None and 'columns' in self.schema:
            for column in self.schema['columns']:
                path = tuple(
                    name for name in column.split('.')[:-1]
                    if name not in ('length', '0')
                )
                if path:
                    self.column_paths.add(path)

        if self.schema is not None:
            properties = self.schema['properties']
            self.schema_links = [
                key for key, prop in properties.items()
                if 'linkTo' in prop or 'linkTo' in prop.get('items', ())
            ]
            self.schema_version = properties.get('schema_version', {}).get('default')

    def __getitem__(self, name):
        try:
            item = self.get(name)
        except KeyError:
            # Just in case we get an unexpected KeyError
            # FIXME: exception logging.
            raise HTTPInternalServerError('Traversal raised KeyError')
        if item is None:
            raise KeyError(name)
        return item

    def __iter__(self, batchsize=1000):
        session = DBSession()
        query = session.query(Resource.rid).filter(
            Resource.item_type == self.item_type
        ).order_by(Resource.rid)

        for rid, in query.yield_per(batchsize):
            yield rid

    def __len__(self):
        session = DBSession()
        query = session.query(Resource.rid).filter(
            Resource.item_type == self.item_type
        )
        return query.count()

    def get(self, name, default=None):
        root = find_root(self)
        resource = root.get_by_uuid(name, None)
        if resource is not None:
            if resource.collection is not self and resource.__parent__ is not self:
                return default
            return resource
        if is_accession(name):
            resource = root.get_by_unique_key('accession', name)
            if resource is not None:
                if resource.collection is not self and resource.__parent__ is not self:
                    return default
                return resource
        if ':' in name:
            resource = root.get_by_unique_key('alias', name)
            if resource is not None:
                if resource.collection is not self and resource.__parent__ is not self:
                    return default
                return resource
        if self.unique_key is not None:
            resource = root.get_by_unique_key(self.unique_key, name)
            if resource is not None:
                if resource.collection is not self and resource.__parent__ is not self:
                    return default
                return resource
        return default

    def add(self, properties):
        uuid = properties.get('uuid', _marker)
        if uuid is _marker:
            uuid = uuid4()
        else:
            uuid = UUID(uuid)
        item = self.Item.create(self, uuid, properties)
        self.after_add(item)
        return item

    def after_add(self, item):
        '''Hook for subclasses'''

    def template_namespace(self, properties, request=None):
        ns = properties.copy()
        ns['properties'] = properties
        ns['collection_uri'] = resource_path(self, '')
        ns['item_type'] = self.item_type
        ns['context'] = self
        ns['root'] = root = find_root(self)
        ns['registry'] = root.registry
        if request is not None:
            ns['permission'] = permission_checker(self, request)
            ns['request'] = request
        return ns

    def __json__(self, request):
        return self.properties.copy()


Item.Collection = Collection


def expand_column(request, obj, subset, path):
    if isinstance(path, basestring):
        path = path.split('.')
    if not path:
        return
    name = path[0]
    remaining = path[1:]
    value = obj.get(name, None)
    if value is None:
        return
    if not remaining:
        subset[name] = value
        return
    if isinstance(value, list):
        if name not in subset:
            subset[name] = [{} for i in range(len(value))]
        for index, member in enumerate(value):
            if not isinstance(member, dict):
                member = request.embed(member, '@@object')
            expand_column(request, member, subset[index], remaining)
    else:
        if name not in subset:
            subset[name] = {}
        if not isinstance(value, dict):
            value = request.embed(value, '@@object')
        expand_column(request, value, subset[name], remaining)


def etag_tid(view_callable):
    def wrapped(context, request):
        result = view_callable(context, request)
        etag = 'tid:%s' % context.model.data[''].propsheet.tid
        request.response.etag = etag
        cache_control = request.response.cache_control
        cache_control.private = True
        cache_control.max_age = 0
        cache_control.must_revalidate = True
        return result

    return wrapped


def if_match_tid(view_callable):
    """ ETag conditional PUT/PATCH support

    Returns 412 Precondition Failed when etag does not match.
    """
    def wrapped(context, request):
        etag = 'tid:%s' % context.model.data[''].propsheet.tid
        if etag not in request.if_match:
            raise HTTPPreconditionFailed("The resource has changed.")
        return view_callable(context, request)

    return wrapped


def load_db(context, request):
    result = {}

    frame = request.params.get('frame', 'columns')

    limit = request.params.get('limit', 25)
    if limit in ('', 'all'):
        limit = None
    if limit is not None:
        try:
            limit = int(limit)
        except ValueError:
            limit = 25

    items = (
        item for item in itervalues(context)
        if request.has_permission('view', item)
    )

    if limit is not None:
        items = islice(items, limit)

    result['@graph'] = [
        request.embed(request.resource_path(item, '@@' + frame))
        for item in items
    ]

    if limit is not None and len(result['@graph']) == limit:
        params = [(k, v) for k, v in request.params.items() if k != 'limit']
        params.append(('limit', 'all'))
        result['all'] = '%s?%s' % (request.resource_path(context), urlencode(params))

    return result


def load_es(context, request):
    from .views.search import search
    result = search(context, request, context.item_type)

    if len(result['@graph']) < result['total']:
        params = [(k, v) for k, v in request.params.items() if k != 'limit']
        params.append(('limit', 'all'))
        result['all'] = '%s?%s' % (request.resource_path(context), urlencode(params))

    return result


@view_config(context=Collection, permission='list', request_method='GET')
def collection_list(context, request):
    properties = context.__json__(request)
    ns = context.template_namespace(properties, request)
    compiled = ObjectTemplate(context.merged_template)
    templated = compiled(ns)
    properties.update(templated)

    uri = ns['collection_uri']
    if request.query_string:
        uri += '?' + request.query_string
    properties['@id'] = uri

    root = find_root(context)
    if context.__name__ in root['pages']:
        properties['default_page'] = embed(
            request, '/pages/%s/@@page' % context.__name__, as_user=True)

    datastore = request.params.get('datastore', None)
    if datastore is None:
        datastore = request.registry.settings.get('collection_datastore', 'database')
    # Switch to change summary page loading options: load_db, load_es
    if datastore == 'elasticsearch':
        result = load_es(context, request)
    else:
        result = load_db(context, request)

    result.update(properties)
    return result


@view_config(context=Collection, permission='add', request_method='POST',
             validators=[validate_item_content_post])
@view_config(context=Collection, permission='add_unvalidated', request_method='POST',
             validators=[no_validate_item_content_post],
             request_param=['validate=false'])
def collection_add(context, request, render=None):
    if render is None:
        render = request.params.get('render', True)
    properties = request.validated
    item = context.add(properties)
    request.registry.notify(Created(item, request))
    if render == 'uuid':
        item_uri = '/%s/' % item.uuid
    else:
        item_uri = request.resource_path(item)
    if asbool(render) is True:
        rendered = embed(request, join(item_uri, '@@details'), as_user=True)
    else:
        rendered = item_uri
    request.response.status = 201
    request.response.location = item_uri
    result = {
        'status': 'success',
        '@type': ['result'],
        '@graph': [rendered],
    }
    return result


@subscriber(ContextFound)
def traversal_security(event):
    """ Check traversal was permitted at each step
    """
    request = event.request
    traversed = reversed(list(lineage(request.context)))
    # Required to view the root page as anonymous user
    # XXX Needs test once login based browser tests work.
    next(traversed)  # Skip root object
    for resource in traversed:
        result = has_permission('traverse', resource, request)
        if not result:
            msg = 'Unauthorized: traversal failed permission check'
            raise HTTPForbidden(msg, result=result)


@view_config(context=Item, permission='view', request_method='GET')
@view_config(context=Item, permission='view', request_method='GET',
             name='details')
def item_view(context, request):
    frame = request.params.get('frame', 'page')
    if getattr(request, '__parent__', None) is None:
        # We need the response headers from non subrequests
        try:
            return render_view_to_response(context, request, name=frame)
        except PredicateMismatch:
            # Avoid this view emitting PredicateMismatch
            exc_class, exc, tb = sys.exc_info()
            exc.__class__ = HTTPNotFound
            raise_with_traceback(exc, tb)
    path = request.resource_path(context, '@@' + frame)
    if request.query_string:

        path += '?' + request.query_string
    return embed(request, path, as_user=True)



def item_links(context, request):
    # This works from the schema rather than the links table
    # so that upgrade on GET can work.
    properties = context.__json__(request)
    root = request.root
    for name in context.schema_links:
        value = properties.get(name, None)
        if value is None:
            continue
        if isinstance(value, list):
            properties[name] = [
                request.resource_path(root.get_by_uuid(v))
                for v in value
            ]
        else:
            properties[name] = request.resource_path(root.get_by_uuid(value))
    return properties


@view_config(context=Item, permission='view', request_method='GET',
             name='object')
def item_view_object(context, request):
    """ Render json structure

    1. Fetch stored properties, possibly upgrading.
    2. Link canonicalization (overwriting uuids.)
    3. Templated properties (including reverse links.)
    """
    properties = item_links(context, request)
    ns = context.template_namespace(properties, request)
    compiled = ObjectTemplate(context.merged_template)
    templated = compiled(ns)
    properties.update(templated)
    return properties


@view_config(context=Item, permission='view', request_method='GET',
             name='embedded')
def item_view_embedded(context, request):
    item_path = request.resource_path(context)
    properties = request.embed(item_path, '@@object')
    for path in context.embedded_paths:
        expand_path(request, properties, path)
    return properties


@view_config(context=Item, permission='view', request_method='GET',
             name='actions')
def item_actions(context, request):
    path = request.resource_path(context)
    ns = {}
    ns['permission'] = permission_checker(context, request)
    ns['item_type'] = context.item_type
    ns['item_uri'] = path
    compiled = ObjectTemplate(context.actions)
    actions = compiled(ns)
    return {
        '@id': path,
        'actions': actions,
    }


@view_config(context=Item, permission='view', request_method='GET',
             name='page')
def item_view_page(context, request):
    item_path = request.resource_path(context)
    properties = request.embed(item_path, '@@embedded')
    actions = request.embed(item_path, '@@actions', as_user=True)['actions']
    if actions:
        properties['actions'] = actions
    if request.has_permission('audit', context):
        properties['audit'] = request.embed(item_path, '@@audit')['audit']
    # XXX Move to view when views on ES results implemented.
    properties = context.expand_page(request, properties)
    return properties


@view_config(context=Item, permission='expand', request_method='GET',
             name='expand')
def item_view_expand(context, request):
    path = request.resource_path(context)
    properties = request.embed(path, '@@object')
    for path in request.params.getall('expand'):
        expand_path(request, properties, path)
    return properties


@view_config(context=Item, permission='view', request_method='GET',
             name='columns')
def item_view_columns(context, request):
    path = request.resource_path(context)
    properties = request.embed(path, '@@object')
    if context.schema is not None and 'columns' in context.schema:
        return properties

    subset = {
        '@id': properties['@id'],
        '@type': properties['@type'],
    }
    for path in context.collection.column_paths:
        expand_column(request, properties, subset, path)

    return subset


@view_config(context=Item, permission='audit', request_method='GET',
             name='audit-self')
def item_view_audit_self(context, request):
    path = request.resource_path(context)
    types = [context.item_type] + context.base_types
    return {
        '@id': path,
        'audit': request.audit(types=types, path=path),
    }


@view_config(context=Item, permission='audit', request_method='GET',
             name='audit')
def item_view_audit(context, request):
    path = request.resource_path(context)
    properties = request.embed(path, '@@object')
    audit = inherit_audits(request, properties, context.audit_inherit_paths)
    return {
        '@id': path,
        'audit': audit,
    }


@view_config(context=Item, permission='view_raw', request_method='GET',
             name='raw')
def item_view_raw(context, request):
    if asbool(request.params.get('upgrade', True)):
        return context.upgrade_properties()
    return context.properties


@view_config(context=Item, permission='edit', request_method='GET',
             name='edit', decorator=etag_tid)
def item_view_edit(context, request):
    return item_links(context, request)


@view_config(context=Item, permission='edit', request_method='PUT',
             validators=[validate_item_content_put], decorator=if_match_tid)
@view_config(context=Item, permission='edit', request_method='PATCH',
             validators=[validate_item_content_patch], decorator=if_match_tid)
@view_config(context=Item, permission='edit_unvalidated', request_method='PUT',
             validators=[no_validate_item_content_put],
             request_param=['validate=false'], decorator=if_match_tid)
@view_config(context=Item, permission='edit_unvalidated', request_method='PATCH',
             validators=[no_validate_item_content_patch],
             request_param=['validate=false'], decorator=if_match_tid)
def item_edit(context, request, render=None):
    """ This handles both PUT and PATCH, difference is the validator

    PUT - replaces the current properties with the new body
    PATCH - updates the current properties with those supplied.
    """
    if render is None:
        render = request.params.get('render', True)
    properties = request.validated
    # This *sets* the property sheet
    request.registry.notify(BeforeModified(context, request))
    context.update(properties)
    request.registry.notify(AfterModified(context, request))
    if render == 'uuid':
        item_uri = '/%s' % context.uuid
    else:
        item_uri = request.resource_path(context)
    if asbool(render) is True:
        rendered = embed(request, join(item_uri, '@@object'), as_user=True)
    else:
        rendered = item_uri
    request.response.status = 200
    result = {
        'status': 'success',
        '@type': ['result'],
        '@graph': [rendered],
    }
    return result


class Created(object):
    def __init__(self, object, request):
        self.object = object
        self.request = request


class BeforeModified(object):
    def __init__(self, object, request):
        self.object = object
        self.request = request


class AfterModified(object):
    def __init__(self, object, request):
        self.object = object
        self.request = request


def path_ids(request, obj, path):
    if isinstance(path, basestring):
        path = path.split('.')
    if not path:
        yield obj if isinstance(obj, basestring) else obj['@id']
        return
    name = path[0]
    remaining = path[1:]
    value = obj.get(name, None)
    if value is None:
        return
    if isinstance(value, list):
        for member in value:
            if remaining and isinstance(member, basestring):
                member = embed(request, join(member, '@@object'))
            for item_uri in path_ids(request, member, remaining):
                yield item_uri
    else:
        if remaining and isinstance(value, basestring):
            value = embed(request, join(value, '@@object'))
        for item_uri in path_ids(request, value, remaining):
            yield item_uri


def inherit_audits(request, embedded, embedded_paths):
    audit_paths = {embedded['@id']}
    for embedded_path in embedded_paths:
        audit_paths.update(path_ids(request, embedded, embedded_path))

    audit = []
    for audit_path in audit_paths:
        result = embed(request, join(audit_path, '@@audit-self'))
        audit.extend(result['audit'])
    return audit


@view_config(context=Item, name='index-data', permission='index', request_method='GET')
def item_index_data(context, request):
    links = defaultdict(list)
    for link in context.model.rels:
        links[link.rel].append(link.target_rid)

    keys = defaultdict(list)
    for key in context.model.unique_keys:
        keys[key.name].append(key.value)

    principals_allowed = {}
    for permission in ('view', 'edit', 'audit'):
        p = principals_allowed_by_permission(context, permission)
        if p is Everyone:
            p = [Everyone]
        principals_allowed[permission] = sorted(p)

    path = resource_path(context)
    paths = {path}
    collection = context.collection

    if collection.unique_key in keys:
        paths.update(
            resource_path(collection, key)
            for key in keys[collection.unique_key])

    for base in (collection, request.root):
        for key_name in ('accession', 'alias'):
            if key_name not in keys:
                continue
            paths.add(resource_path(base, str(context.uuid)))
            paths.update(
                resource_path(base, key)
                for key in keys[key_name])

    path = path + '/'
    embedded = embed(request, join(path, '@@embedded'))
    audit = inherit_audits(request, embedded, context.audit_inherit_paths)

    document = {
        'embedded': embedded,
        'object': embed(request, join(path, '@@object')),
        'links': links,
        'keys': keys,
        'principals_allowed': principals_allowed,
        'paths': sorted(paths),
        'audit': audit,
        'embedded_uuids': sorted(request._embedded_uuids),
        'linked_uuids': sorted(request._linked_uuids),
    }

    return document
