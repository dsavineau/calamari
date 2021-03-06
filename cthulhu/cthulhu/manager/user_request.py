import logging
import uuid

from salt.client import LocalClient

from cthulhu.manager import config
from cthulhu.log import log
from calamari_common.types import OsdMap, PgSummary, USER_REQUEST_COMPLETE, USER_REQUEST_SUBMITTED
from cthulhu.util import now


class PublishError(Exception):
    pass


class UserRequestBase(object):
    """
    A request acts on one or more Ceph-managed objects, i.e.
    mon, mds, osd, pg.

    Amist the terminology mess of 'jobs', 'commands', 'operations', this class
    is named for clarity: it's an operation at an end-user level of
    granularity, something that might be a button in the UI.

    UserRequests are usually remotely executed on a mon.  However, there
    may be a final step of updating the state of ClusterMonitor in order
    that subsequent REST API consumer reads return values consistent with
    the job having completed, e.g. waiting for the OSD map to be up
    to date before calling a pool creation complete.  For this reason,
    UserRequests have a local ID and completion state that is independent
    of their remote ID (salt jid).  UserRequests may also execute more than
    one JID in the course of their lifetime.

    Requests have the following lifecycle:
     NEW object is created, it has all the information needed to do its job
         other than where it should execute.
     SUBMITTED the request has started executing, usually this will have involved sending
               out a salt job, so .jid is often set but not always.
     COMPLETE no further action, this instance will remain constant from this point on.
              this does not indicate anything about success or failure.
    """

    NEW = 'new'
    SUBMITTED = USER_REQUEST_SUBMITTED
    COMPLETE = USER_REQUEST_COMPLETE
    states = [NEW, SUBMITTED, COMPLETE]

    def __init__(self, fsid, cluster_name, commands):
        """
        Requiring cluster_name and fsid is redundant (ideally everything would
        speak in terms of fsid) but convenient, because the librados interface
        wants a cluster name when you create a client, and otherwise we would
        have to look up via ceph.conf.
        """
        # getChild isn't in 2.6
        logname = '.'.join((log.name, self.__class__.__name__))
        self.log = logging.getLogger(logname)
        self.requested_at = now()
        self.completed_at = None

        # This is actually kind of overkill compared with having a counter,
        # somewhere but it's easy.
        self.id = uuid.uuid4().__str__()

        self._minion_id = None
        self._fsid = fsid
        self._cluster_name = cluster_name
        self._commands = commands

        self.jid = None

        self.state = self.NEW
        self.result = None
        self.error = False
        self.error_message = ""

        # Time at which we last believed the current JID to be really running
        self.alive_at = None

    def set_error(self, message):
        self.error = True
        self.error_message = message

    @property
    def minion_id(self):
        return self._minion_id

    @property
    def associations(self):
        """
        A dictionary of Event-compatible assocations for this request, indicating
        which cluster/server/services we are affecting.
        """
        return {}

    @property
    def headline(self):
        """
        Single line describing what the request is trying to accomplish.
        """
        raise NotImplementedError()

    @property
    def status(self):
        """
        Single line describing which phase of the request is currently happening, useful
        to distinguish what's going on for long running operations.  For simple quick
        operations no need to return anything here as the headline tells all.
        """
        if self.state != self.COMPLETE:
            return "Running"
        elif self.error:
            return "Failed (%s)" % self.error_message
        else:
            return "Completed successfully"

    @property
    def awaiting_versions(self):
        """
        Requests indicate that they are waiting for particular sync objects, optionally
        specifying the particular version they are waiting for (otherwise set version
        to None).

        :return dict of SyncObject subclass to (version or None)
        """
        return {}

    def submit(self, minion_id):
        """
        Start remote execution phase by publishing a job to salt.
        """
        assert self.state == self.NEW

        self._minion_id = minion_id
        self._submit(self._commands)

        self.state = self.SUBMITTED

    def _submit(self, commands):
        self.log.debug("Request._submit: %s/%s/%s" % (self._minion_id, self._cluster_name, commands))

        client = LocalClient(config.get('cthulhu', 'salt_config_path'))
        pub_data = client.run_job(self._minion_id, 'ceph.rados_commands',
                                  [self._fsid, self._cluster_name, commands])
        if not pub_data:
            # FIXME: LocalClient uses 'print' to record the
            # details of what went wrong :-(
            raise PublishError("Failed to publish job")

        self.log.info("Request %s started job %s" % (self.id, pub_data['jid']))

        self.alive_at = now()
        self.jid = pub_data['jid']

        return self.jid

    def complete_jid(self, result):
        """
        Call this when remote execution is done.

        Implementations must always update .jid appropriately here: either to the
        jid of a new job, or to None.
        """
        self.result = result
        self.log.info("Request %s JID %s completed with result=%s" % (self.id, self.jid, self.result))
        self.jid = None

        # This is a default behaviour for UserRequests which don't override this method:
        # assume completion of a JID means the job is now done.
        self.complete()

    def complete(self):
        """
        Call this when you're all done
        """
        assert self.state != self.COMPLETE
        assert self.jid is None

        self.log.info("Request %s completed with error=%s (%s)" % (self.id, self.error, self.error_message))
        self.state = self.COMPLETE
        self.completed_at = now()

    def on_map(self, sync_type, sync_objects):
        """
        It is only valid to call this for sync_types which are currently in awaiting_versions
        """
        pass


class UserRequest(UserRequestBase):

    def __init__(self, headline, fsid, cluster_name, commands):
        super(UserRequest, self).__init__(fsid, cluster_name, commands)
        self._await_version = None
        self._headline = headline

    @property
    def headline(self):
        return self._headline


class OsdMapModifyingRequest(UserRequestBase):
    """
    Specialization of UserRequest which waits for Calamari's copy of
    the OsdMap sync object to catch up after execution of RADOS commands.
    """

    def __init__(self, headline, fsid, cluster_name, commands):
        super(OsdMapModifyingRequest, self).__init__(fsid, cluster_name, commands)
        self._await_version = None
        self._headline = headline

    @property
    def headline(self):
        return self._headline

    @property
    def status(self):
        if self.state != self.COMPLETE and self._await_version:
            return "Waiting for OSD map epoch %s" % self._await_version
        else:
            return super(OsdMapModifyingRequest, self).status

    @property
    def associations(self):
        return {
            'fsid': self._fsid
        }

    @property
    def awaiting_versions(self):
        if self._await_version and self.state != self.COMPLETE:
            return {
                OsdMap: self._await_version
            }
        else:
            return {}

    def complete_jid(self, result):
        # My remote work is done, record the version of the map that I will wait for
        # and start waiting for it.
        self.jid = None
        self.result = result
        self._await_version = result['versions']['osd_map']

    def on_map(self, sync_type, sync_objects):
        assert sync_type == OsdMap
        assert self._await_version is not None

        osd_map = sync_objects.get(OsdMap)
        ready = osd_map.version >= self._await_version
        if ready:
            self.log.debug("check passed (%s >= %s)" % (osd_map.version, self._await_version))
            self.complete()
        else:
            self.log.debug("check pending (%s < %s)" % (osd_map.version, self._await_version))


class PoolCreatingRequest(OsdMapModifyingRequest):
    """
    Like an OsdMapModifyingRequest, but additionally wait for all PGs in the resulting pool
    to leave state 'creating' before completing.
    """

    def __init__(self, headline, fsid, cluster_name, pool_name, commands):
        super(PoolCreatingRequest, self).__init__(headline, fsid, cluster_name, commands)
        self._awaiting_pgs = False
        self._pool_name = pool_name

        self._pool_id = None
        self._pg_count = None

    @property
    def awaiting_versions(self):
        if self._awaiting_pgs:
            return {PgSummary: None}
        elif self._await_version:
            return {OsdMap: self._await_version}
        else:
            return {}

    def on_map(self, sync_type, sync_objects):
        if self._awaiting_pgs:
            assert sync_type == PgSummary
            pg_summary = sync_objects.get(PgSummary)
            pgs_not_creating = 0
            for state_tuple, count in pg_summary.data['by_pool'][self._pool_id].items():
                states = state_tuple.split("+")
                if 'creating' not in states:
                    pgs_not_creating += count

            if pgs_not_creating >= self._pg_count:
                self.complete()

        elif self._await_version:
            assert sync_type == OsdMap
            osd_map = sync_objects.get(OsdMap)
            if osd_map.version >= self._await_version:
                for pool_id, pool in osd_map.pools_by_id.items():
                    if pool['pool_name'] == self._pool_name:
                        self._pool_id = pool_id
                        self._pg_count = pool['pg_num']
                        break

                if self._pool_id is None:
                    log.error("'{0}' not found, pools are {1}".format(
                        self._pool_name, [p['pool_name'] for p in osd_map.pools_by_id.values()]
                    ))
                    self.set_error("Expected pool '{0}' not found".format(self._pool_name))
                    self.complete()

                self._awaiting_pgs = True
        else:
            raise NotImplementedError("Unexpected map {0}".format(sync_type))


class PgProgress(object):
    """
    Encapsulate the state that PgCreatingRequest uses for splitting up
    creation operations into blocks.
    """
    def __init__(self, initial, final, block_size):
        self.initial = initial
        self.final = final
        self._block_size = block_size

        self._still_to_create = self.final - self.initial

        self._intermediate_goal = self.initial
        self.advance_goal()

    def advance_goal(self):
        assert not self.is_final_block()
        self._intermediate_goal = min(self.final, self._intermediate_goal + self._block_size)

    def set_created_pg_count(self, pg_count):
        self._still_to_create = max(self.final - pg_count, 0)

    def get_status(self):
        total_creating = (self.final - self.initial)
        created = total_creating - self._still_to_create

        if self._intermediate_goal != self.final:
            currently_creating_min = max(self._intermediate_goal - self._block_size, self.initial)
            currently_creating_max = self._intermediate_goal
            return "Waiting for PG creation (%s/%s), currently creating PGs %s-%s" % (
                created, total_creating, currently_creating_min, currently_creating_max)
        else:
            return "Waiting for PG creation (%s/%s)" % (created, total_creating)

    def expected_count(self):
        """
        After a successful 'osd pool set' operation, what should pg_num be?
        """
        return self._intermediate_goal

    def is_final_block(self):
        """
        Is the current expansion under way the final one?
        """
        return self._intermediate_goal == self.final

    def is_complete(self):
        """
        Have all expected PGs been created?
        """
        return self._still_to_create == 0

    @property
    def goal(self):
        return self._intermediate_goal


class PgCreatingRequest(OsdMapModifyingRequest):
    """
    Specialization of OsdMapModifyingRequest to issue a request
    to issue a second set of commands after PGs created by an
    initial set of commands have left the 'creating' state.

    This handles issuing multiple smaller "osd pool set pg_num" calls when
    the number of new PGs requested is greater than mon_osd_max_split_count,
    caller is responsible for telling us how many we may create at once.
    """

    # Simple state machine for phases:
    #  - have send a job, waiting for JID to complete
    #  - a jid completed, waiting for corresponding OSD map update
    #  - OSD map has updated, waiting for created PGs to leave state 'creating'
    JID_WAIT = 'jid_wait'
    OSD_MAP_WAIT = 'osd_map_wait'
    PG_MAP_WAIT = 'pg_map_wait'

    def __init__(self, headline, fsid, cluster_name, commands,
                 pool_id, pool_name, pgp_num,
                 initial_pg_count, final_pg_count, block_size):
        """
        :param commands: Commands to execute before creating PGs
        :param initial_pg_count: How many PGs the pool has before we change anything
        :param final_pg_count: How many PGs the pool should have when we are done
        :param block_size: How many PGs we may create in one "osd pool set" command
        """

        self._await_osd_version = None

        self._pool_id = pool_id
        self._pool_name = pool_name
        self._headline = headline

        self._pg_progress = PgProgress(initial_pg_count, final_pg_count, block_size)
        if initial_pg_count != final_pg_count:
            commands.append(('osd pool set', {
                'pool': self._pool_name,
                'var': 'pg_num',
                'val': self._pg_progress.goal
            }))
            self._post_create_commands = [("osd pool set", {'pool': pool_name, 'var': 'pgp_num', 'val': pgp_num})]

        super(PgCreatingRequest, self).__init__(headline, fsid, cluster_name, commands)
        self._phase = self.JID_WAIT

    @property
    def status(self):
        if not self.state == self.COMPLETE and not self._pg_progress.is_complete():
            return self._pg_progress.get_status()
        else:
            return super(PgCreatingRequest, self).status

    def complete_jid(self, result):
        # No matter at what stage, the next thing to do after completing
        # a JID is to wait for the resulting change to the OSD map
        assert self._phase == self.JID_WAIT
        self.jid = None
        self.result = result
        self._await_version = result['versions']['osd_map']
        self._phase = self.OSD_MAP_WAIT

    @property
    def awaiting_versions(self):
        if self._phase == self.JID_WAIT:
            return {}
        elif self._phase == self.OSD_MAP_WAIT:
            return {
                OsdMap: self._await_version
            }
        elif self._phase == self.PG_MAP_WAIT:
            return {
                PgSummary: None,
                OsdMap: None
            }

    def on_map(self, sync_type, sync_objects):
        self.log.debug("PgCreatingRequest %s %s" % (sync_type.str, self._phase))
        if self._phase == self.PG_MAP_WAIT:
            if sync_type == PgSummary:
                # Count the PGs in this pool which are not in state 'creating'
                pg_summary = sync_objects.get(PgSummary)
                pgs_not_creating = 0
                for state_tuple, count in pg_summary.data['by_pool'][self._pool_id].items():
                    states = state_tuple.split("+")
                    if 'creating' not in states:
                        pgs_not_creating += count

                self._pg_progress.set_created_pg_count(pgs_not_creating)
                self.log.debug("PgCreatingRequest.on_map: pg_counter=%s/%s (final %s)" % (
                    pgs_not_creating, self._pg_progress.goal, self._pg_progress.final))
                if pgs_not_creating >= self._pg_progress.goal:
                    if self._pg_progress.is_final_block():
                        self.log.debug("PgCreatingRequest.on_map Creations complete")
                        if self._post_create_commands:
                            self.log.debug("PgCreatingRequest.on_map Issuing post-create commands")
                            self._submit(self._post_create_commands)
                            self._phase = self.JID_WAIT
                        else:
                            self.log.debug("PgCreatingRequest.on_map All done")
                            self.complete()
                    else:
                        self.log.debug("PgCreatingREQUEST.on_map Issuing more creates")
                        self._pg_progress.advance_goal()
                        # Request another tranche of PGs up to _block_size
                        self._submit([('osd pool set', {
                            'pool': self._pool_name,
                            'var': 'pg_num',
                            'val': self._pg_progress.goal
                        })])
                        self._phase = self.JID_WAIT
            elif sync_type == OsdMap:
                # Keep an eye on the OsdMap to check that pg_num is what we expect: otherwise
                # if forces of darkness changed pg_num then our PG creation check could
                # get confused and fail to complete.
                osd_map = sync_objects.get(OsdMap)
                pool = osd_map.pools_by_id[self._pool_id]
                if pool['pg_num'] != self._pg_progress.expected_count():
                    self.set_error("PG creation interrupted (unexpected change to pg_num)")
                    self.complete()
                    return
            else:
                raise NotImplementedError("Unexpected map {1} in state {2}".format(
                    sync_type, self._phase
                ))

        elif self._phase == self.OSD_MAP_WAIT:
            # Read back the pg_num for my pool from the OSD map
            osd_map = sync_objects.get(OsdMap)
            pool = osd_map.pools_by_id[self._pool_id]

            # In Ceph <= 0.67.7, "osd pool set pg_num" will return success even if it hasn't
            # really increased pg_num, so we must examine the OSD map to see if it really succeded
            if pool['pg_num'] != self._pg_progress.expected_count():
                self.set_error("PG creation failed (check that there aren't already PGs in 'creating' state)")
                self.complete()
                return

            assert self._await_version
            ready = osd_map.version >= self._await_version
            if ready:
                # OSD map advancement either means a PG creation round completed, or that
                # the post_create_commands completed.  Distinguish by looking at pg_progress.
                if self._pg_progress.is_complete():
                    # This was the OSD map update from the post_create_commands, we we're all done!
                    self.complete()
                else:
                    # This was the OSD map update from a PG creation command, so start waiting
                    # for the pgs
                    self._phase = self.PG_MAP_WAIT

        else:
            raise NotImplementedError("Unexpected {0} in phase {1}".format(sync_type, self._phase))
