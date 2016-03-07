#    Copyright 2015 SimpliVity Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import versionutils
from oslo_versionedobjects import fields

from cinder import db
from cinder import exception
from cinder.i18n import _
from cinder import objects
from cinder.objects import base

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class MetadataObject(dict):
    # This is a wrapper class that simulates SQLAlchemy (.*)Metadata objects to
    # maintain compatibility with older representations of Volume that some
    # drivers rely on. This is helpful in transition period while some driver
    # methods are invoked with volume versioned object and some SQLAlchemy
    # object or dict.
    def __init__(self, key=None, value=None):
        super(MetadataObject, self).__init__()
        self.key = key
        self.value = value

    def __getattr__(self, name):
        if name in self:
            return self[name]
        else:
            raise AttributeError("No such attribute: " + name)

    def __setattr__(self, name, value):
        self[name] = value


@base.CinderObjectRegistry.register
class Volume(base.CinderPersistentObject, base.CinderObject,
             base.CinderObjectDictCompat, base.CinderComparableObject):
    # Version 1.0: Initial version
    # Version 1.1: Added metadata, admin_metadata, volume_attachment, and
    #              volume_type
    # Version 1.2: Added glance_metadata, consistencygroup and snapshots
    # Version 1.3: Added finish_volume_migration()
    VERSION = '1.3'

    OPTIONAL_FIELDS = ('metadata', 'admin_metadata', 'glance_metadata',
                       'volume_type', 'volume_attachment', 'consistencygroup',
                       'snapshots')

    DEFAULT_EXPECTED_ATTR = ('admin_metadata', 'metadata')

    fields = {
        'id': fields.UUIDField(),
        '_name_id': fields.UUIDField(nullable=True),
        'ec2_id': fields.UUIDField(nullable=True),
        'user_id': fields.UUIDField(nullable=True),
        'project_id': fields.UUIDField(nullable=True),

        'snapshot_id': fields.UUIDField(nullable=True),

        'host': fields.StringField(nullable=True),
        'size': fields.IntegerField(nullable=True),
        'availability_zone': fields.StringField(nullable=True),
        'status': fields.StringField(nullable=True),
        'attach_status': fields.StringField(nullable=True),
        'migration_status': fields.StringField(nullable=True),

        'scheduled_at': fields.DateTimeField(nullable=True),
        'launched_at': fields.DateTimeField(nullable=True),
        'terminated_at': fields.DateTimeField(nullable=True),

        'display_name': fields.StringField(nullable=True),
        'display_description': fields.StringField(nullable=True),

        'provider_id': fields.UUIDField(nullable=True),
        'provider_location': fields.StringField(nullable=True),
        'provider_auth': fields.StringField(nullable=True),
        'provider_geometry': fields.StringField(nullable=True),

        'volume_type_id': fields.UUIDField(nullable=True),
        'source_volid': fields.UUIDField(nullable=True),
        'encryption_key_id': fields.UUIDField(nullable=True),

        'consistencygroup_id': fields.UUIDField(nullable=True),

        'deleted': fields.BooleanField(default=False, nullable=True),
        'bootable': fields.BooleanField(default=False, nullable=True),
        'multiattach': fields.BooleanField(default=False, nullable=True),

        'replication_status': fields.StringField(nullable=True),
        'replication_extended_status': fields.StringField(nullable=True),
        'replication_driver_data': fields.StringField(nullable=True),

        'previous_status': fields.StringField(nullable=True),

        'metadata': fields.DictOfStringsField(nullable=True),
        'admin_metadata': fields.DictOfStringsField(nullable=True),
        'glance_metadata': fields.DictOfStringsField(nullable=True),
        'volume_type': fields.ObjectField('VolumeType', nullable=True),
        'volume_attachment': fields.ObjectField('VolumeAttachmentList',
                                                nullable=True),
        'consistencygroup': fields.ObjectField('ConsistencyGroup',
                                               nullable=True),
        'snapshots': fields.ObjectField('SnapshotList', nullable=True),
    }

    # NOTE(thangp): obj_extra_fields is used to hold properties that are not
    # usually part of the model
    obj_extra_fields = ['name', 'name_id', 'volume_metadata',
                        'volume_admin_metadata', 'volume_glance_metadata']

    @property
    def name_id(self):
        return self.id if not self._name_id else self._name_id

    @name_id.setter
    def name_id(self, value):
        self._name_id = value

    @property
    def name(self):
        return CONF.volume_name_template % self.name_id

    # TODO(dulek): Three properties below are for compatibility with dict
    # representation of volume. The format there is different (list of
    # SQLAlchemy models) so we need a conversion. Anyway - these should be
    # removed when we stop this class from deriving from DictObjectCompat.
    @property
    def volume_metadata(self):
        md = [MetadataObject(k, v) for k, v in self.metadata.items()]
        return md

    @volume_metadata.setter
    def volume_metadata(self, value):
        md = {d['key']: d['value'] for d in value}
        self.metadata = md

    @property
    def volume_admin_metadata(self):
        md = [MetadataObject(k, v) for k, v in self.admin_metadata.items()]
        return md

    @volume_admin_metadata.setter
    def volume_admin_metadata(self, value):
        md = {d['key']: d['value'] for d in value}
        self.admin_metadata = md

    @property
    def volume_glance_metadata(self):
        md = [MetadataObject(k, v) for k, v in self.glance_metadata.items()]
        return md

    @volume_glance_metadata.setter
    def volume_glance_metadata(self, value):
        md = {d['key']: d['value'] for d in value}
        self.glance_metadata = md

    def __init__(self, *args, **kwargs):
        super(Volume, self).__init__(*args, **kwargs)
        self._orig_metadata = {}
        self._orig_admin_metadata = {}
        self._orig_glance_metadata = {}

        self._reset_metadata_tracking()

    def obj_reset_changes(self, fields=None):
        super(Volume, self).obj_reset_changes(fields)
        self._reset_metadata_tracking(fields=fields)

    def _reset_metadata_tracking(self, fields=None):
        if fields is None or 'metadata' in fields:
            self._orig_metadata = (dict(self.metadata)
                                   if 'metadata' in self else {})
        if fields is None or 'admin_metadata' in fields:
            self._orig_admin_metadata = (dict(self.admin_metadata)
                                         if 'admin_metadata' in self
                                         else {})
        if fields is None or 'glance_metadata' in fields:
            self._orig_glance_metadata = (dict(self.glance_metadata)
                                          if 'glance_metadata' in self
                                          else {})

    def obj_what_changed(self):
        changes = super(Volume, self).obj_what_changed()
        if 'metadata' in self and self.metadata != self._orig_metadata:
            changes.add('metadata')
        if ('admin_metadata' in self and
                self.admin_metadata != self._orig_admin_metadata):
            changes.add('admin_metadata')
        if ('glance_metadata' in self and
                self.glance_metadata != self._orig_glance_metadata):
            changes.add('glance_metadata')

        return changes

    def obj_make_compatible(self, primitive, target_version):
        """Make an object representation compatible with a target version."""
        super(Volume, self).obj_make_compatible(primitive, target_version)
        target_version = versionutils.convert_version_to_tuple(target_version)

    @staticmethod
    def _from_db_object(context, volume, db_volume, expected_attrs=None):
        if expected_attrs is None:
            expected_attrs = []
        for name, field in volume.fields.items():
            if name in Volume.OPTIONAL_FIELDS:
                continue
            value = db_volume.get(name)
            if isinstance(field, fields.IntegerField):
                value = value or 0
            volume[name] = value

        # Get data from db_volume object that was queried by joined query
        # from DB
        if 'metadata' in expected_attrs:
            metadata = db_volume.get('volume_metadata', [])
            volume.metadata = {item['key']: item['value'] for item in metadata}
        if 'admin_metadata' in expected_attrs:
            metadata = db_volume.get('volume_admin_metadata', [])
            volume.admin_metadata = {item['key']: item['value']
                                     for item in metadata}
        if 'glance_metadata' in expected_attrs:
            metadata = db_volume.get('volume_glance_metadata', [])
            volume.glance_metadata = {item['key']: item['value']
                                      for item in metadata}
        if 'volume_type' in expected_attrs:
            db_volume_type = db_volume.get('volume_type')
            if db_volume_type:
                volume.volume_type = objects.VolumeType._from_db_object(
                    context, objects.VolumeType(), db_volume_type,
                    expected_attrs='extra_specs')
        if 'volume_attachment' in expected_attrs:
            attachments = base.obj_make_list(
                context, objects.VolumeAttachmentList(context),
                objects.VolumeAttachment,
                db_volume.get('volume_attachment'))
            volume.volume_attachment = attachments
        if 'consistencygroup' in expected_attrs:
            consistencygroup = objects.ConsistencyGroup(context)
            consistencygroup._from_db_object(context,
                                             consistencygroup,
                                             db_volume['consistencygroup'])
            volume.consistencygroup = consistencygroup
        if 'snapshots' in expected_attrs:
            snapshots = base.obj_make_list(
                context, objects.SnapshotList(context),
                objects.Snapshot,
                db_volume['snapshots'])
            volume.snapshots = snapshots

        volume._context = context
        volume.obj_reset_changes()
        return volume

    @base.remotable
    def create(self):
        if self.obj_attr_is_set('id'):
            raise exception.ObjectActionError(action='create',
                                              reason=_('already created'))
        updates = self.cinder_obj_get_changes()

        if 'consistencygroup' in updates:
            raise exception.ObjectActionError(
                action='create', reason=_('consistencygroup assigned'))
        if 'snapshots' in updates:
            raise exception.ObjectActionError(
                action='create', reason=_('snapshots assigned'))

        db_volume = db.volume_create(self._context, updates)
        self._from_db_object(self._context, self, db_volume)

    @base.remotable
    def save(self):
        updates = self.cinder_obj_get_changes()
        if updates:
            if 'consistencygroup' in updates:
                raise exception.ObjectActionError(
                    action='save', reason=_('consistencygroup changed'))
            if 'glance_metadata' in updates:
                raise exception.ObjectActionError(
                    action='save', reason=_('glance_metadata changed'))
            if 'snapshots' in updates:
                raise exception.ObjectActionError(
                    action='save', reason=_('snapshots changed'))
            if 'metadata' in updates:
                # Metadata items that are not specified in the
                # self.metadata will be deleted
                metadata = updates.pop('metadata', None)
                self.metadata = db.volume_metadata_update(self._context,
                                                          self.id, metadata,
                                                          True)
            if self._context.is_admin and 'admin_metadata' in updates:
                metadata = updates.pop('admin_metadata', None)
                self.admin_metadata = db.volume_admin_metadata_update(
                    self._context, self.id, metadata, True)

            db.volume_update(self._context, self.id, updates)
            self.obj_reset_changes()

    @base.remotable
    def destroy(self):
        with self.obj_as_admin():
            db.volume_destroy(self._context, self.id)

    def obj_load_attr(self, attrname):
        if attrname not in self.OPTIONAL_FIELDS:
            raise exception.ObjectActionError(
                action='obj_load_attr',
                reason=_('attribute %s not lazy-loadable') % attrname)
        if not self._context:
            raise exception.OrphanedObjectError(method='obj_load_attr',
                                                objtype=self.obj_name())

        if attrname == 'metadata':
            self.metadata = db.volume_metadata_get(self._context, self.id)
        elif attrname == 'admin_metadata':
            self.admin_metadata = {}
            if self._context.is_admin:
                self.admin_metadata = db.volume_admin_metadata_get(
                    self._context, self.id)
        elif attrname == 'glance_metadata':
            try:
                # NOTE(dulek): We're using alias here to have conversion from
                # list to dict done there.
                self.volume_glance_metadata = db.volume_glance_metadata_get(
                    self._context, self.id)
            except exception.GlanceMetadataNotFound:
                # NOTE(dulek): DB API raises when volume has no
                # glance_metadata. Silencing this because at this level no
                # metadata is a completely valid result.
                self.glance_metadata = {}
        elif attrname == 'volume_type':
            # If the volume doesn't have volume_type, VolumeType.get_by_id
            # would trigger a db call which raise VolumeTypeNotFound exception.
            self.volume_type = (objects.VolumeType.get_by_id(
                self._context, self.volume_type_id) if self.volume_type_id
                else None)
        elif attrname == 'volume_attachment':
            attachments = objects.VolumeAttachmentList.get_all_by_volume_id(
                self._context, self.id)
            self.volume_attachment = attachments
        elif attrname == 'consistencygroup':
            consistencygroup = objects.ConsistencyGroup.get_by_id(
                self._context, self.consistencygroup_id)
            self.consistencygroup = consistencygroup
        elif attrname == 'snapshots':
            self.snapshots = objects.SnapshotList.get_all_for_volume(
                self._context, self.id)

        self.obj_reset_changes(fields=[attrname])

    def delete_metadata_key(self, key):
        db.volume_metadata_delete(self._context, self.id, key)
        md_was_changed = 'metadata' in self.obj_what_changed()

        del self.metadata[key]
        self._orig_metadata.pop(key, None)

        if not md_was_changed:
            self.obj_reset_changes(['metadata'])

    def finish_volume_migration(self, dest_volume):
        # We swap fields between source (i.e. self) and destination at the
        # end of migration because we want to keep the original volume id
        # in the DB but now pointing to the migrated volume.
        skip = ({'id', 'provider_location', 'glance_metadata'} |
                set(self.obj_extra_fields))
        for key in set(dest_volume.fields.keys()) - skip:
            # Only swap attributes that are already set.  We do not want to
            # unexpectedly trigger a lazy-load.
            if not dest_volume.obj_attr_is_set(key):
                continue

            value = getattr(dest_volume, key)
            value_to_dst = getattr(self, key)

            # Destination must have a _name_id since the id no longer matches
            # the volume.  If it doesn't have a _name_id we set one.
            if key == '_name_id':
                if not dest_volume._name_id:
                    setattr(dest_volume, key, self.id)
                continue
            elif key == 'migration_status':
                value = None
                value_to_dst = 'deleting'
            elif key == 'display_description':
                value_to_dst = 'migration src for ' + self.id
            elif key == 'status':
                value_to_dst = 'deleting'

            setattr(self, key, value)
            setattr(dest_volume, key, value_to_dst)

        dest_volume.save()
        return dest_volume


@base.CinderObjectRegistry.register
class VolumeList(base.ObjectListBase, base.CinderObject):
    VERSION = '1.1'

    fields = {
        'objects': fields.ListOfObjectsField('Volume'),
    }

    child_versions = {
        '1.0': '1.0',
        '1.1': '1.1',
    }

    @base.remotable_classmethod
    def get_all(cls, context, marker, limit, sort_keys=None, sort_dirs=None,
                filters=None, offset=None):
        volumes = db.volume_get_all(context, marker, limit,
                                    sort_keys=sort_keys, sort_dirs=sort_dirs,
                                    filters=filters, offset=offset)
        expected_attrs = ['admin_metadata', 'metadata']
        return base.obj_make_list(context, cls(context), objects.Volume,
                                  volumes, expected_attrs=expected_attrs)

    @base.remotable_classmethod
    def get_all_by_host(cls, context, host, filters=None):
        volumes = db.volume_get_all_by_host(context, host, filters)
        expected_attrs = ['admin_metadata', 'metadata']
        return base.obj_make_list(context, cls(context), objects.Volume,
                                  volumes, expected_attrs=expected_attrs)

    @base.remotable_classmethod
    def get_all_by_group(cls, context, group_id, filters=None):
        volumes = db.volume_get_all_by_group(context, group_id, filters)
        expected_attrs = ['admin_metadata', 'metadata']
        return base.obj_make_list(context, cls(context), objects.Volume,
                                  volumes, expected_attrs=expected_attrs)

    @base.remotable_classmethod
    def get_all_by_project(cls, context, project_id, marker, limit,
                           sort_keys=None, sort_dirs=None, filters=None,
                           offset=None):
        volumes = db.volume_get_all_by_project(context, project_id, marker,
                                               limit, sort_keys=sort_keys,
                                               sort_dirs=sort_dirs,
                                               filters=filters, offset=offset)
        expected_attrs = ['admin_metadata', 'metadata']
        return base.obj_make_list(context, cls(context), objects.Volume,
                                  volumes, expected_attrs=expected_attrs)
