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
"""Base class and convenience utilities for functional placement tests."""

import copy

import os_resource_classes as orc
from oslo_utils.fixture import uuidsentinel as uuids
from oslo_utils import uuidutils

from placement.db import graph_db as db
from placement import exception
from placement.objects import allocation as alloc_obj
from placement.objects import consumer as consumer_obj
from placement.objects import inventory as inv_obj
from placement.objects import project as project_obj
from placement.objects import research_context as res_ctx
from placement.objects import resource_provider as rp_obj
from placement.objects import trait as trait_obj
from placement.objects import user as user_obj
from placement.tests.functional import base


DISK_INVENTORY = dict(
    total=200,
    reserved=10,
    min_unit=2,
    max_unit=5,
    step_size=1,
    allocation_ratio=1.0,
    resource_class=orc.DISK_GB
)

DISK_ALLOCATION = dict(
    consumer_id=uuids.disk_consumer,
    used=2,
    resource_class=orc.DISK_GB
)


def create_provider(context, name, *aggs, **kwargs):
    parent = kwargs.get('parent')
    uuid = kwargs.get('uuid', getattr(uuids, name))
    rp = rp_obj.ResourceProvider(context, name=name, uuid=uuid)
    if parent:
        rp.parent_provider_uuid = parent
    rp.create()
    if aggs:
        rp.set_aggregates(aggs)
    return rp


def set_sharing_among_agg(rp):
    """Given a sharing provider, sets the [:ASSOCIATED] relationship with any
    other resource providers in the aggregates that the sharing provider is in.

    This is a bit of a hack to get things working with graph relationships,
    given the previous, even hackier, approach of using the
    MISC_SHARES_VIA_AGGREGATE trait as a sharing flag.
    """
    # First, get the aggregates that the sharing provider is associated with.
    agg_uuids = rp.get_aggregates()
    if not agg_uuids:
        return
    providers = res_ctx.provider_ids_matching_aggregates(rp._context,
            [agg_uuids])
    if not providers:
        return
    # Now set the ASSOCIATED relationship.
    rp_obj.associate(rp._context, rp, providers)


def add_inventory(rp, rc, total, **kwargs):
    kwargs.setdefault('max_unit', total)
    inv = inv_obj.Inventory(rp, resource_class=rc, total=total, **kwargs)
    rp.add_inventory(inv)
    return inv


def set_traits(rp, *traits):
    tlist = []
    for tname in traits:
        trait = None
        try:
            trait = trait_obj.Trait.get_by_name(rp._context, tname)
        except exception.TraitNotFound:
            trait = trait_obj.Trait(rp._context, name=tname)
            trait.create()
        tlist.append(trait)
    rp.set_traits(tlist)
    return tlist


def ensure_consumer(ctx, user, project, consumer_uuid=None):
    # NOTE(efried): If not specified, use a random consumer UUID - we don't
    # want to override any existing allocations from the test case.
    consumer_uuid = consumer_uuid or uuidutils.generate_uuid()
    try:
        consumer = consumer_obj.Consumer.get_by_uuid(ctx, consumer_uuid)
    except exception.NotFound:
        consumer = consumer_obj.Consumer(ctx, uuid=consumer_uuid, user=user,
                project=project)
        consumer.create()
    consumer_obj.relate_project_and_user(ctx, project.uuid, user.uuid,
            consumer_uuid)
    return consumer


def set_allocation(ctx, rp, consumer, rc_used_dict):
    alloc = [
        alloc_obj.Allocation(
            resource_provider=rp, resource_class=rc,
            consumer=consumer, used=used)
        for rc, used in rc_used_dict.items()
    ]
    alloc_obj.replace_all(ctx, alloc)
    return alloc


class PlacementDbBaseTestCase(base.TestCase):

    def setUp(self):
        super(PlacementDbBaseTestCase, self).setUp()
        # we use context in some places and ctx in other. We should only use
        # context, but let's paper over that for now.
        self.ctx = self.context
        self.user_obj = user_obj.User(self.ctx, uuid=uuids.user)
        self.user_obj.create()
        self.project_obj = project_obj.Project(self.ctx, uuid=uuids.project)
        self.project_obj.create()
        # For debugging purposes, populated by _create_provider and used by
        # _validate_allocation_requests to make failure results more readable.
        self.rp_uuid_to_name = {}

    def _assert_traits(self, expected_traits, traits_objs):
        expected_traits.sort()
        traits = []
        for obj in traits_objs:
            traits.append(obj.name)
        traits.sort()
        self.assertEqual(expected_traits, traits)

    def _assert_traits_in(self, expected_traits, traits_objs):
        traits = [trait.name for trait in traits_objs]
        for expected in expected_traits:
            self.assertIn(expected, traits)

    def _create_provider(self, name, *aggs, **kwargs):
        rp = create_provider(self.ctx, name, *aggs, **kwargs)
        self.rp_uuid_to_name[rp.uuid] = name
        return rp

    def get_provider_uuid_by_name(self, name):
        rp_ids = [k for k, v in self.rp_uuid_to_name.items() if v == name]
        if not len(rp_ids) == 1:
            raise Exception
        return rp_ids[0]

    def allocate_from_provider(self, rp, rc, used, consumer_id=None,
                               consumer=None):
        if consumer is None:
            consumer = ensure_consumer(
                self.ctx, self.user_obj, self.project_obj, consumer_id)
        alloc_list = set_allocation(self.ctx, rp, consumer, {rc: used})
        return alloc_list

    def _make_allocation(self, inv_dict, alloc_dict):
        alloc_dict = copy.copy(alloc_dict)
        rp = self._create_provider('allocation_resource_provider')
        disk_inv = inv_obj.Inventory(resource_provider=rp, **inv_dict)
        rp.set_inventory([disk_inv])
        consumer_id = alloc_dict.pop('consumer_id')
        consumer = ensure_consumer(
            self.ctx, self.user_obj, self.project_obj, consumer_id)
        alloc = alloc_obj.Allocation(
            resource_provider=rp, consumer=consumer, **alloc_dict)
        alloc_obj.replace_all(self.ctx, [alloc])
        return rp, alloc

    def create_aggregate(self, agg_uuid):
        conn = self.placement_db.get_engine().connect()
        ins_stmt = rp_obj._AGG_TBL.insert().values(uuid=agg_uuid)
        res = conn.execute(ins_stmt)
        agg_id = res.inserted_primary_key[0]
        return agg_id
