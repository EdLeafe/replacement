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

from oslo_db import exception as db_exc
from oslo_utils import timeutils

from placement.db import graph_db as db
from placement import db_api
from placement import exception
from placement.objects import project as project_obj
from placement.objects import user as user_obj


@db_api.placement_context_manager.writer
def create_incomplete_consumers(ctx, batch_size):
    """Finds all the consumer records that are missing for allocations and
    creates consumer records for them, using the "incomplete consumer" project
    and user CONF options.

    Returns a tuple containing two identical elements with the number of
    consumer records created, since this is the expected return format for data
    migration routines.

    # NOTE (edleafe): NOT NEEDED WITH GRAPH DBs. This is a migration process
    # that should be run before converting to a graph DB.
    """
    user_uuid = user_obj.ensure_incomplete_user(ctx)
    # Find all the consumers with no relation to a user, and then set the
    # relation to the incomplete_user.
    query = """
            MATCH (u:USER {uuid: '%s'})
            MATCH p=()-[:OWNS]->(cs:CONSUMER)
            WITH u, cs, size(relationships(p)) AS numrel
            WITH u, cs, sum(numrel) AS total_rel
            WHERE total_rel = 0
            MERGE (u)->[:OWNS]->(cs)
            RETURN cs
    """ % user_uuid
    ctx.tx.run(query)


@db_api.placement_context_manager.writer
def delete_consumers_if_no_allocations(ctx, consumer_uuids):
    """Looks to see if any of the supplied consumers has any allocations and if
    not, deletes the consumer record entirely.

    :param ctx: `placement.context.RequestContext` that
                contains an oslo_db Session
    :param consumer_uuids: UUIDs of the consumers to check and maybe delete
    """
    # Delete consumers that have no usages
    return
    query = """
            MATCH (cs:CONSUMER)
            WHERE NOT (cs)-[:USES]->() 
            WITH cs
            DETACH DELETE cs
    """
    ctx.tx.run(query)


@db_api.placement_context_manager.reader
def _get_consumer_by_uuid(ctx, uuid):
    """Return information about the consumer and its related project and user.
    """
    query = """
            MATCH (cs:CONSUMER {uuid: '%s'})
            WITH cs
            OPTIONAL MATCH (pj:PROJECT)-[:OWNS]->(cs)
            WITH cs, pj
            OPTIONAL MATCH (u:USER)-[:BELONGS_TO]->(pj)
            RETURN cs, pj, u
    """ % uuid
    result = ctx.tx.run(query).data()
    if not result:
        raise exception.ConsumerNotFound(uuid=uuid)
    rec = result[0]
    cs = db.pythonize(rec["cs"])
    pj = rec["pj"]
    pj = db.pythonize(pj) if pj else None
    user = rec["u"]
    user = db.pythonize(user) if user else None
    return {"uuid": cs.uuid,
            "project_uuid": pj,
            "user_uuid": user,
            "generation": cs.get("generation"),
            "updated_at": cs.get("updated_at"),
            "created_at": cs.get("created_at"),
    }


@db_api.placement_context_manager.writer
def _delete_consumer(ctx, consumer):
    """Deletes the supplied consumer. If the consumer has any allocations
    against resources, those will also be deleted.

    :param ctx: `placement.context.RequestContext` that contains an oslo_db
                Session
    :param consumer: `Consumer` whose generation should be updated.
    """
    query = """
            MATCH (cs:CONSUMER {uuid: '%s'})
            DETACH DELETE cs
    """ % consumer.uuid
    ctx.tx.run(query)


@db_api.placement_context_manager.writer
def relate_project_and_user(ctx, project_uuid, user_uuid, consumer_uuid):
    """After the project, user, and consumer are established for an
    allocation, make sure that the relationships between them are set.
    """
    # First, remove any existing relationships if they aren't the same
    query = """
            MATCH pth_user=(pj)-[:OWNS]->(us:USER {uuid: '%s'})
            RETURN pj.uuid AS own_uuid
            UNION
            MATCH pth_cons=(us)-[:OWNS]->(co:CONSUMER {uuid: '%s'})
            RETURN us.uuid AS own_uuid
            """ % (user_uuid, consumer_uuid)
    result = ctx.tx.run(query).data()
    owners = [rec["own_uuid"] for rec in result]
    if owners == [project_uuid, user_uuid]:
        # Everything's already related
        return
    if owners:
        # Delete any :OWNS relationships to the user and consumers if they
        # don't match the desired relationships.
        query = """
                MATCH (us:USER {uuid: '%s'})
                WITH us
                MATCH pth_u=(pj:PROJECT)-[:OWNS]-(us)
                WHERE pj.uuid <> '%s'
                WITH relationships(pth_u) AS urels
                UNWIND urels AS urel
                DELETE urel
        """ % (user_uuid, project_uuid)
        result = ctx.tx.run(query).data()
        query = """
                MATCH (co:CONSUMER {uuid: '%s'})
                WITH co
                MATCH pth_c=(us:USER)-[:OWNS]-(co)
                WHERE us.uuid <> '%s'
                WITH relationships(pth_c) AS crels
                UNWIND crels AS crel
                DELETE crel
        """ % (consumer_uuid, user_uuid)
        result = ctx.tx.run(query).data()
    # Now create the relationships
    query = """
            MATCH (pj:PROJECT {uuid: '%s'})
            MATCH (us:USER {uuid: '%s'})
            MATCH (co:CONSUMER {uuid: '%s'})
            WITH pj, us, co
            MERGE (pj)-[:OWNS]->(us)
            MERGE (us)-[:OWNS]->(co)
            RETURN pj, us, co
    """ % (project_uuid, user_uuid, consumer_uuid)
    ctx.tx.run(query).data()


class Consumer(object):

    def __init__(self, context, uuid=None, project=None, user=None,
                 generation=None, updated_at=None, created_at=None):
        self._context = context
        self.uuid = uuid
        self.project = project
        self.user = user
        self.generation = generation
        self.updated_at = updated_at
        self.created_at = created_at

    @staticmethod
    def _from_db_object(ctx, target, source):
        target.uuid = source['uuid']
        target.generation = source['generation']
        target.created_at = source['created_at']
        target.updated_at = source['updated_at']

        target.project = project_obj.Project(ctx, uuid=source["project_uuid"])
        target.user = user_obj.User(ctx, uuid=source["user_uuid"])
        target._context = ctx
        return target

    @classmethod
    def get_by_uuid(cls, ctx, uuid):
        res = _get_consumer_by_uuid(ctx, uuid)
        return cls._from_db_object(ctx, cls(ctx), res)

    def create(self):
        @db_api.placement_context_manager.writer
        def _create_in_db(ctx, gen):
            creat = self.created_at if self.created_at else "timestamp()"
            updt = self.updated_at if self.updated_at else "timestamp()"
            query = """
                    MERGE (cs:CONSUMER {uuid: '%s', generation: %s,
                        created_at: %s, updated_at: %s})
                    RETURN cs
            """ % (self.uuid, gen, creat, updt)
            ctx.tx.run(query)
        gen = self.generation or 0
        _create_in_db(self._context, gen)
        self.generation = gen

    def update(self):
        """Used to update the consumer's project and user information without
        incrementing the consumer's generation. Since the relation is
        project->user->consumer, we only need to update the user->consumer
        relationship.
        """
        @db_api.placement_context_manager.writer
        def _update_in_db(ctx):
            user_uuid = self.user.uuid if self.user else None
            if user_uuid:
                query = """
                        MATCH p=(u:USER)-[:OWNS]-(cs:CONSUMER {uuid: '%s',
                            generation: %s})
                        WITH cs, relationships(p)[0] AS owns
                        DELETE owns
                        WITH cs
                        OPTIONAL MATCH (u:USER {uuid: '%s'})
                        WITH u, cs
                        CREATE (u)-[:OWNS]->(cs)
                """ % (self.uuid, self.generation, user_uuid)
            else:
                query = """
                        MATCH p=(u:USER)-[:OWNS]-(cs:CONSUMER {uuid: '%s',
                            generation: %s})
                        WITH cs, relationships(p)[0] AS owns
                        DELETE owns
                """ % (self.uuid, self.generation, user_uuid)
            ctx.tx.run(query)
        _update_in_db(self._context)

    def increment_generation(self):
        """Increments the consumer's generation.

        :raises placement.exception.ConcurrentUpdateDetected: if another thread
            updated the same consumer's view of its allocations in between the
            time when this object was originally read and the call which
            modified the consumer's state (e.g. replacing allocations for a
            consumer)
        """
        consumer_gen = self.generation
        new_generation = consumer_gen + 1
        query = """
                MATCH (cs:CONSUMER {uuid: '%s', generation: %s})
                WITH cs
                SET cs.generation = %s
                RETURN cs
        """ % (self.uuid, consumer_gen, new_generation)
        result = self._context.tx.run(query).data()
        if not result:
            raise exception.ConcurrentUpdateDetected
        self.generation = new_generation

    def delete(self):
        _delete_consumer(self._context, self)
