from collections import defaultdict
import logging
import math
import os
import random
from retrying import retry
import sqlite3
import uuid

from lobster import util

logger = logging.getLogger('lobster.unit')

# FIXME these are hardcoded in some SQL statements below.  SQLite does not
# seem to have the concept of variables...

# Status
INITIALIZED = 0
ASSIGNED = 1
SUCCESSFUL = 2
FAILED = 3
ABORTED = 4
PUBLISHED = 6
MERGING = 7
MERGED = 8

# Task type
PROCESS = 0
MERGE = 1

TaskUpdate = util.record('TaskUpdate',
                'bytes_bare_output',
                'bytes_output',
                'bytes_received',
                'bytes_sent',
                'cache',
                'cache_end_size',
                'cache_start_size',
                'exit_code',
                'events_read',
                'events_written',
                'host',
                'units_processed',
                'limits_exceeded',
                'memory_resident',
                'memory_swap',
                'memory_virtual',
                'status',
                'submissions',
                'time_submit',
                'time_transfer_in_start',
                'time_transfer_in_end',
                'time_wrapper_start',
                'time_wrapper_ready',
                'time_stage_in_end',
                'time_prologue_end',
                'time_file_requested',
                'time_file_opened',
                'time_file_processing',
                'time_processing_end',
                'time_epilogue_end',
                'time_stage_out_end',
                'time_transfer_out_start',
                'time_transfer_out_end',
                'time_retrieved',
                'time_on_worker',
                'time_total_on_worker',
                'time_cpu',
                'workdir_footprint',
                'workdir_num_files',
                'id',
                default=0)


class UnitStore:
    def __init__(self, config):
        self.uuid = str(uuid.uuid4()).replace('-', '')
        self.db_path = os.path.join(config['workdir'], "lobster.db")
        self.db = sqlite3.connect(self.db_path)

        self.__failure_threshold = config.get("threshold for failure", 10)
        self.__skipping_threshold = config.get("threshold for skipping", 10)

        self.db.execute("""create table if not exists workflows(
            cfg text,
            dataset text,
            empty_source int,
            events int default 0,
            file_based int,
            global_tag text,
            id integer primary key autoincrement,
            units integer,
            units_done int default 0,
            units_left int default 0,
            units_paused int default 0,
            units_running int default 0,
            taskruntime int default null,
            tasksize int,
            label text,
            masked_lumis int default 0,
            merged int default 0,
            path text,
            pset_hash text default null,
            publish_label text,
            release text,
            uuid text)""")
        self.db.execute("""create table if not exists tasks(
            bytes_bare_output int default 0,
            bytes_output int default 0,
            bytes_received int,
            bytes_sent int,
            cache int,
            cache_end_size int,
            cache_start_size int,
            workflow int,
            id integer primary key autoincrement,
            events_read int default 0,
            events_written int default 0,
            exit_code int,
            failed int default 0,
            host text,
            task int,
            units int default 0,
            units_processed int default 0,
            limits_exceeded text,
            memory_resident int,
            memory_virtual int,
            memory_swap int,
            published_file_block text,
            status int default 0,
            submissions int default 0,
            time_submit int,
            time_transfer_in_start int,
            time_transfer_in_end int,
            time_wrapper_start int,
            time_wrapper_ready int,
            time_stage_in_end int,
            time_prologue_end int,
            time_file_requested int,
            time_file_opened int,
            time_file_processing int,
            time_processing_end int,
            time_epilogue_end int,
            time_stage_out_end int,
            time_transfer_out_start int,
            time_transfer_out_end int,
            time_retrieved int,
            time_on_worker int,
            time_total_on_worker int,
            time_cpu int,
            type int,
            workdir_footprint int,
            workdir_num_files int,
            foreign key(workflow) references workflows(id))""")

        self.db.commit()

    def disconnect(self):
        self.db.close()

    def register(self, dataset_cfg, dataset_info, taskruntime=None):
        label = dataset_cfg['label']
        unique_args = dataset_cfg.get('unique parameters', [None])

        cur = self.db.cursor()
        cur.execute("""insert into workflows
                       (dataset,
                       label,
                       path,
                       release,
                       global_tag,
                       publish_label,
                       cfg,
                       uuid,
                       file_based,
                       empty_source,
                       tasksize,
                       taskruntime,
                       units,
                       masked_lumis,
                       units_left,
                       events)
                       values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                           dataset_cfg.get('dataset', label),
                           label,
                           dataset_info.path,
                           os.path.basename(os.environ.get('LOCALRT', '')),
                           dataset_cfg.get('global tag'),
                           dataset_cfg.get('publish label', dataset_cfg['label']).replace('-', '_'), #TODO: more lexical checks #TODO: publish label check
                           dataset_cfg.get('cmssw config'),
                           self.uuid,
                           dataset_info.file_based,
                           dataset_info.empty_source,
                           dataset_info.tasksize,
                           taskruntime,
                           dataset_info.total_lumis * len(unique_args),
                           dataset_info.masked_lumis,
                           dataset_info.total_lumis * len(unique_args),
                           dataset_info.total_events))

        self.db.execute("""create table if not exists files_{0}(
            id integer primary key autoincrement,
            filename text,
            skipped int default 0,
            units int,
            units_done int default 0,
            units_running int default 0,
            events int,
            events_read int default 0,
            bytes int default 0)""".format(label))

        cur.execute("""create table if not exists units_{0}(
            id integer primary key autoincrement,
            task integer,
            run integer,
            lumi integer,
            file integer,
            status integer default 0,
            failed integer default 0,
            arg text,
            foreign key(task) references tasks(id),
            foreign key(file) references files_{0}(id))""".format(label))

        for fn in dataset_info.files:
            file_lumis = len(dataset_info.lumis[fn])
            cur.execute(
                    """insert into files_{0}(units, events, filename, bytes) values (?, ?, ?, ?)""".format(label), (
                        file_lumis * len(unique_args),
                        dataset_info.event_counts[fn],
                        fn,
                        dataset_info.filesizes[fn]))
            file_id = cur.lastrowid

            for arg in unique_args:
                columns = [(file_id, run, lumi, arg) for (run, lumi) in dataset_info.lumis[fn]]
                self.db.executemany("insert into units_{0}(file, run, lumi, arg) values (?, ?, ?, ?)".format(label), columns)

        self.db.execute("create index if not exists index_filename_{0} on files_{0}(filename)".format(label))
        self.db.execute("create index if not exists index_events_{0} on units_{0}(run, lumi)".format(label))
        self.db.execute("create index if not exists index_files_{0} on units_{0}(file)".format(label))

        self.db.commit()

    def pop_units(self, num=1):
        """
        Create a predetermined number of tasks.  The task these are
        created for is drawn randomly from all unfinished tasks.

        Arguments:
            num: the number of tasks to be created (default 1)
        Returns:
            a list containing an id, workflow label, file information (id,
            filename), lumi information (id, file id, run, lumi)
        """

        rows = [xs for xs in self.db.execute("""
            select label, id, units_left, units_left * 1. / tasksize, tasksize, empty_source
            from workflows
            where units_left > 0""")]
        if len(rows) == 0:
            return []

        # calculate how many tasks we can create from all workflows, still
        tasks_left = sum(int(math.ceil(tasks)) for _, _, _, tasks, _, _ in rows)
        tasks = []

        random.shuffle(rows)

        # if total tasks left < requested tasks, make the tasks smaller to
        # keep all workers occupied
        if tasks_left < num:
            taper = float(tasks_left) / num
            for workflow, workflow_id, units_left, ntasks, tasksize, empty_source in rows:
                tasksize = max(math.ceil((taper * tasksize)), 1)
                size = [int(tasksize)] * max(1, int(math.ceil(ntasks / taper)))
                tasks.extend(self.__pop_units(size, workflow, workflow_id, empty_source))
        else:
            for workflow, workflow_id, units_left, ntasks, tasksize, empty_source in rows:
                size = [int(tasksize)] * max(1, int(math.ceil(ntasks * num / tasks_left)))
                tasks.extend(self.__pop_units(size, workflow, workflow_id, empty_source))
        return tasks

    @retry(stop_max_attempt_number=10)
    def __pop_units(self, size, workflow, workflow_id, empty_source):
        """Internal method to create tasks from a workflow
        """
        logger.debug("creating {0} task(s) for workflow {1}".format(len(size), workflow))

        with self.db:
            fileinfo = list(self.db.execute("""select id, filename
                        from files_{0}
                        where
                            (units_done + units_running < units) and
                            (skipped < ?)
                        order by skipped asc""".format(workflow), (self.__skipping_threshold,)))
            files = [x for (x, y) in fileinfo]
            fileinfo = dict(fileinfo)

            rows = []
            for i in range(0, len(files), 40):
                chunk = files[i:i + 40]
                rows.extend(self.db.execute("""
                    select id, file, run, lumi, arg, failed
                    from units_{0}
                    where file in ({1}) and status not in (1, 2, 6, 7, 8)
                    """.format(workflow, ', '.join('?' for _ in chunk)), chunk))

            # files and lumis for individual tasks
            files = set()
            units = []

            # lumi veto to avoid duplicated processing
            all_lumis = set()

            # task container and current task size
            tasks = []
            current_size = 0

            def insert_task(files, units, arg):
                cur = self.db.cursor()
                cur.execute("insert into tasks(workflow, status, type) values (?, 1, 0)", (workflow_id,))
                task_id = cur.lastrowid

                tasks.append((
                    str(task_id),
                    workflow,
                    [(id, fileinfo[id]) for id in files],
                    units,
                    arg,
                    empty_source,
                    False))

            for id, file, run, lumi, arg, failed in rows:
                if (run, lumi) in all_lumis or failed > self.__failure_threshold:
                    continue

                if current_size == 0:
                    if len(size) == 0:
                        break

                if failed == self.__failure_threshold:
                    insert_task([file], [(id, file, run, lumi)], arg)
                    continue

                if lumi > 0:
                    all_lumis.add((run, lumi))
                    for (ls_id, ls_file, ls_run, ls_lumi) in self.db.execute("""
                            select
                                id, file, run, lumi
                            from
                                units_{0}
                            where
                                run=? and
                                lumi=? and
                                status not in (1, 2, 6, 7, 8) and
                                failed < ?""".format(workflow),
                            (run, lumi, self.__failure_threshold)):
                        units.append((ls_id, ls_file, ls_run, ls_lumi))
                        files.add(ls_file)
                else:
                    units.append((id, file, run, lumi))
                    files.add(file)

                current_size += 1

                if current_size == size[0]:
                    insert_task(files, units, arg)

                    files = set()
                    units = []

                    current_size = 0
                    size.pop(0)

            if current_size > 0:
                insert_task(files, units, arg)

            workflow_update = []
            file_update = defaultdict(int)
            task_update = defaultdict(int)
            unit_update = []

            for (task, label, files, units, arg, empty_source, merge) in tasks:
                workflow_update += units
                task_update[task] = len(units)
                unit_update += [(task, id) for (id, file, run, lumi) in units]
                for (id, filename) in files:
                    file_update[id] += len(filter(lambda tpl: tpl[1] == id, units))

            self.db.execute(
                    "update workflows set units_running=(units_running + ?) where id=?",
                    (len(workflow_update), workflow_id))

            self.db.executemany("update files_{0} set units_running=(units_running + ?) where id=?".format(workflow),
                    [(v, k) for (k, v) in file_update.items()])
            self.db.executemany("update tasks set units=? where id=?",
                    [(v, k) for (k, v) in task_update.items()])
            self.db.executemany("update units_{0} set status=1, task=? where id=?".format(workflow),
                    unit_update)

            return tasks if len(unit_update) > 0 else []

    def reset_units(self):
        with self.db as db:
            ids = [id for (id,) in db.execute("select id from tasks where status=1")]
            db.execute("update workflows set units_running=0, merged=0")
            db.execute("update tasks set status=4 where status=1")
            db.execute("update tasks set status=2 where status=7")
            for (label, dset_id) in db.execute("select label, id from workflows"):
                db.execute("update files_{0} set units_running=0".format(label))
                db.execute("update units_{0} set status=4 where status=1".format(label))
                db.execute("update units_{0} set status=2 where status=7".format(label))
                self.update_workflow_stats(label)
        return ids

    @retry(stop_max_attempt_number=10)
    def update_units(self, taskinfos):
        task_updates = []

        with self.db:
            for ((dset, unit_source), updates) in taskinfos.items():
                file_updates = []
                unit_updates = []
                unit_fail_updates = []
                unit_generic_updates = []

                for (task_update, file_update, unit_update) in updates:
                    task_updates.append(task_update)
                    file_updates += file_update

                    # units either fail or are successful
                    # FIXME this should really go into the task handler
                    if unit_source == 'tasks':
                        unit_status = SUCCESSFUL if task_update.status == FAILED else MERGED
                    else:
                        unit_status = FAILED if task_update.status == FAILED else SUCCESSFUL

                    if task_update.status == FAILED:
                        unit_fail_updates.append((task_update.id,))

                    unit_updates += unit_update
                    unit_generic_updates.append((unit_status, task_update.id))

                # update all units of the tasks
                self.db.executemany("""update {0} set
                    status=?
                    where task=?""".format(unit_source),
                    unit_generic_updates)

                # update selected, missed units
                self.db.executemany("""update {0} set
                    status=?
                    where id=?""".format(unit_source),
                    unit_updates)

                # increment failed counter
                if len(unit_fail_updates) > 0:
                    self.db.executemany("""update {0} set
                        failed=failed + 1
                        where task=?""".format(unit_source),
                        unit_fail_updates)

                # update files in the workflow
                if len(file_updates) > 0:
                    self.db.executemany("""update files_{0} set
                        units_running=(select count(*) from units_{0} where status==1 and file=files_{0}.id),
                        units_done=(select count(*) from units_{0} where status==2 and file=files_{0}.id),
                        events_read=(events_read + ?),
                        skipped=(skipped + ?)
                        where id=?""".format(dset),
                        file_updates)

            query = "update tasks set {0} where id=?".format(TaskUpdate.sql_fragment(stop=-1))
            self.db.executemany(query, task_updates)

            for label, _ in taskinfos.keys():
                self.update_workflow_stats(label)

    def update_workflow_stats(self, label):
        id, size, targettime = self.db.execute("select id, tasksize, taskruntime from workflows where label=?", (label,)).fetchone()

        if targettime is not None:
            # Adjust tasksize based on time spend in prologue, processing, and
            # epilogue.  Only do so when difference is > 10%
            tasks, unittime = self.db.execute("""
                select
                    count(*),
                    avg((time_epilogue_end - time_stage_in_end) * 1. / units)
                from tasks where status in (2, 6, 7, 8) and workflow=1 and type=0""").fetchone()

            if tasks > 10:
                bettersize = max(1, int(math.ceil(targettime / unittime)))
                if abs(float(bettersize - size) / size) > .1:
                    logger.info("adjusting task size for {0} from {1} to {2}".format(label, size, bettersize))
                    self.db.execute("update workflows set tasksize=? where id=?", (bettersize, id))

        self.db.execute("""
            update workflows set
                units_running=(select count(*) from units_{0} where status == 1),
                units_done=(select count(*) from units_{0} where status in (2, 6, 7, 8)),
                units_paused=(select count(*) from units_{0}
                        where
                            (failed > ? or file in (select id from files_{0} where skipped >= ?))
                            and status in (0, 3, 4))
            where label=?""".format(label), (self.__failure_threshold, self.__skipping_threshold, label,))

        self.db.execute("""
            update workflows set
                units_left=units - (units_running + units_done + units_paused)
            where label=?""".format(label), (label,))

    def merged(self):
        unmerged = self.db.execute("select count(*) from workflows where merged <> 1").fetchone()[0]
        return unmerged == 0

    def estimate_tasks_left(self):
        rows = [xs for xs in self.db.execute("""
            select label, id, units_left, units_left * 1. / tasksize, tasksize, empty_source
            from workflows
            where units_left > 0""")]
        if len(rows) == 0:
            return 0

        return sum(int(math.ceil(tasks)) for _, _, _, tasks, _, _ in rows)

    def unfinished_units(self):
        cur = self.db.execute("select sum(units - units_done - units_paused) from workflows")
        res = cur.fetchone()[0]
        return 0 if res is None else res

    def running_units(self):
        cur = self.db.execute("select sum(units_running) from workflows")
        return cur.fetchone()[0]

    def workflow_info(self, label):
        cur = self.db.execute("""select workflow,
            path,
            release,
            global_tag,
            publish_label,
            cfg,
            pset_hash,
            id,
            uuid
            from workflows
            where label=?""", (label,))

        return cur.fetchone()

    def workflow_status(self):
        cursor = self.db.execute("""
            select
                label,
                events,
                (select sum(events_read) from tasks where status in (2, 6, 8) and type = 0 and workflow = workflows.id),
                (select sum(events_written) from tasks where status in (2, 6, 8) and type = 0 and workflow = workflows.id),
                units + masked_lumis,
                units,
                units_done,
                units_paused,
                '' || round(
                        units_done * 100.0 / units,
                    1) || ' %'
            from workflows""")
        return ["label events read written units unmasked done paused percent".split()] + list(cursor)

    def pop_unmerged_tasks(self, bytes, num=1):
        """Create merging tasks.

        This creates `num` merge tasks with a maximal size of `bytes`.
        """

        if bytes <= 0:
            return []

        rows = self.db.execute("""
            select label, id, units_done + units_paused == units
            from workflows
            where
                merged <> 1 and
                (units_done + units_paused) * 10 >= units
                and (select count(*) from tasks where workflow=workflows.id and status=2) > 0
        """).fetchall()

        if len(rows) == 0:
            logger.debug("no merge possibility found")
            return []

        random.shuffle(rows)

        res = []
        for dset, dset_id, complete in rows:
            res.extend(self.__pop_unmerged_tasks(dset, dset_id, complete, bytes, num))
            if len(res) > num:
                break
        return res

    @retry(stop_max_attempt_number=10)
    def __pop_unmerged_tasks(self, workflow, dset_id, units_complete, bytes, num=1):
        """Internal method to merge tasks
        """

        logger.debug("trying to merge tasks from {0}".format(workflow))

        class Merge(object):
            def __init__(self, task, units, size, maxsize):
                self.tasks = [task]
                self.units = units
                self.size = size
                self.maxsize = maxsize
            def __cmp__(self, other):
                return cmp(self.size, other.size)
            def add(self, task, units, size):
                if self.size + size > self.maxsize:
                    return False
                self.size += size
                self.units += units
                self.tasks.append(task)
                return True
            def left(self):
                return self.maxsize - self.size

        with self.db:
            # Select the finished processing tasks from the task
            rows = self.db.execute("""
                select id, units, bytes_bare_output
                from tasks
                where status=? and workflow=? and type=0
                order by bytes_bare_output desc""", (SUCCESSFUL, dset_id)).fetchall()

            # If we don't have enough rows, or the smallest two tasks can't be
            # merge, set this up so that the loop below is not evaluted and we
            # skip to the check if the merge for this workflow is complete for
            # the given maximum size.
            if len(rows) < 2 or rows[-2][1] + rows[-1][1] > bytes:
                rows = []
            else:
                minsize = rows[-1][1]

            candidates = []
            for task, units, size in rows:
                # Try to add the current task to a merge, in increasing order of
                # size left
                for merge in reversed(sorted(candidates)):
                    if merge.add(task, units, size):
                        break
                else:
                    # If we're too large to merge, we're skipped
                    if size + minsize <= bytes:
                        candidates.append(Merge(task, units, size, bytes))

            merges = []
            for merge in reversed(sorted(candidates)):
                if len(merge.tasks) == 1:
                    continue
                # For one iteration only: merge if we are either close enough
                # to the target size (TODO maybe this threshold should be
                # configurable? FIXME it's a magic number, anyways) or we are
                # done processing the task, when we merge everything we can.
                if units_complete or merge.size >= bytes * 0.9:
                    merges.append(merge)

            logger.debug("created {0} merge tasks".format(len(merges)))

            if len(merges) == 0 and units_complete:
                rows = self.db.execute("""select count(*) from tasks where status=1 and workflow=?""", (dset_id,)).fetchone()
                if rows[0] == 0:
                    logger.debug("fully merged {0}".format(workflow))
                    self.db.execute("""update workflows set merged=1 where id=?""", (dset_id,))
                    return []

            res = []
            merge_update = []
            for merge in merges:
                merge_id = self.db.execute("""
                    insert into
                    tasks(workflow, units, status, type)
                    values (?, ?, ?, ?)""", (dset_id, merge.units, ASSIGNED, MERGE)).lastrowid
                logger.debug("inserted merge task {0} with tasks {1}".format(merge_id, ", ".join(map(str, merge.tasks))))
                res += [(str(merge_id), workflow, [], [(id, None, -1, -1) for id in merge.tasks], "", False, True)]
                merge_update += [(merge_id, id) for id in merge.tasks]

            self.db.executemany("update tasks set status=7, task=? where id=?", merge_update)
            self.update_workflow_stats(workflow)

            return res

    def update_published(self, block):
        unmerged = [(name, task) for (name, task, merge_task) in block]
        unit_update = [task for (name, task, merge_task) in block]

        with self.db:
            self.db.executemany("""update tasks
                set status=6,
                published_file_block=?
                where id=?""", unmerged)

            self.db.executemany("""update tasks
                set status=6,
                published_file_block=?
                where task=?""", unmerged)

            for task, workflow in self.db.execute("""select tasks.id,
                workflows.label
                from tasks, workflows
                where tasks.id in ({0})
                and tasks.workflow=workflows.id""".format(", ".join(unit_update))):
                self.db.execute("update units_{0} set status=6 where task=?".format(workflow), (task,))

    def successful_tasks(self, label):
        dset_id = self.db.execute("select id from workflows where label=?", (label,)).fetchone()[0]

        cur = self.db.execute("""
            select id, type
            from tasks
            where status=2 and workflow=?
            """, (dset_id,))

        return cur

    def merged_tasks(self, label):
        dset_id = self.db.execute("select id from workflows where label=?", (label,)).fetchone()[0]

        cur = self.db.execute("""select id, type
            from tasks
            where status=8 and workflow=?
            """, (dset_id,))

        return cur

    def failed_tasks(self, label):
        dset_id = self.db.execute("select id from workflows where label=?", (label,)).fetchone()[0]
        cur = self.db.execute("""select id, type
            from tasks
            where status in (3, 4) and workflow=?
            """, (dset_id,))

        return cur

    def failed_units(self, label):
        tasks = self.db.execute("select task from units_{0} where failed > ?".format(label), (self.__failure_threshold,))
        return [xs[0] for xs in tasks]

    def running_tasks(self):
        cur = self.db.execute("select id from tasks where status=1")
        for (v,) in cur:
            yield v

    def skipped_files(self, label):
        files = self.db.execute("select filename from files_{0} where skipped > ?".format(label), (self.__skipping_threshold,))
        return [xs[0] for xs in files]

    def update_pset_hash(self, pset_hash, workflow):
        with self.db as conn:
            conn.execute("update workflows set pset_hash=? where label=?", (pset_hash, workflow))

    @retry(stop_max_attempt_number=10)
    def update_missing(self, tasks):
        with self.db:
            for task, workflow in self.db.execute("""select tasks.id,
                workflows.label
                from tasks, workflows
                where tasks.id in ({0})
                and tasks.workflow=workflows.id""".format(", ".join(map(str, tasks)))):
                self.db.execute("update units_{0} set status=3 where task=?".format(workflow), (task,))

            # update tasks to be failed
            self.db.executemany("update tasks set status=3 where id=?", [(task,) for task in tasks])
            # reset merged tasks from merging
            self.db.executemany("update tasks set status=2 where task=?", [(task,) for task in tasks])
