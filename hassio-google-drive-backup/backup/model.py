from .snapshots import AbstractSnapshot, Snapshot, DummySnapshot
from .config import Config
from .time import Time
from .logbase import LogBase
from .backupscheme import GenerationalScheme, OldestScheme
from .trigger import Trigger
from .exceptions import DeleteMutlipleSnapshotsError, SimulatedError
from .globalinfo import GlobalInfo
from .settings import Setting

from datetime import datetime, timedelta
from typing import TypeVar, Generic, List, Dict, Optional, Tuple
from io import IOBase

T = TypeVar('T')


class CreateOptions(object):
    def __init__(self, when: datetime, name_template: str, retain_sources: Dict[str, bool] = {}):
        self.when: datetime = when
        self.name_template: str = name_template
        self.retain_sources: Dict[str, bool] = retain_sources


class SnapshotSource(Trigger, Generic[T]):
    def __init__(self):
        super().__init__()
        pass

    def name(self) -> str:
        return "Unnamed"

    def enabled(self) -> bool:
        return True

    def upload(self) -> bool:
        return True

    def create(self, options: CreateOptions) -> T:
        pass

    def get(self) -> Dict[str, T]:
        pass

    def delete(self, snapshot: T):
        pass

    def save(self, snapshot: AbstractSnapshot, bytes: IOBase) -> T:
        pass

    def read(self, snapshot: T) -> IOBase:
        pass

    def retain(self, snapshot: T, retain: bool) -> None:
        pass

    def maxCount(self) -> None:
        return 0


class Model(LogBase):
    def __init__(self, config: Config, time: Time, source: SnapshotSource[AbstractSnapshot], dest: SnapshotSource[AbstractSnapshot], info: GlobalInfo, snapshots={}):
        self.config: Config = config
        self.time = time
        self.source: SnapshotSource = source
        self.dest: SnapshotSource = dest
        self.reinitialize()
        self.snapshots: Dict[str, Snapshot] = snapshots
        self.firstSync = True
        self.info = info
        self.simulate_error = None

    def reinitialize(self):
        self._time_of_day: Optional[Tuple[int, int]] = self._parseTimeOfDay()

        # SOMEDAY: this should be cached in config and regenerated on config updates, not here
        self.generational_config = self.config.getGenerationalConfig()

    def getTimeOfDay(self):
        return self._time_of_day

    def _nextSnapshot(self, now: datetime, last_snapshot: Optional[datetime]) -> Optional[datetime]:
        if self.config.get(Setting.DAYS_BETWEEN_SNAPSHOTS) <= 0:
            return None
        if not last_snapshot:
            if self.dest.enabled():
                return now - timedelta(minutes=1)
            else:
                return None

        timeofDay = self.getTimeOfDay()
        if not timeofDay:
            return last_snapshot + timedelta(days=self.config.get(Setting.DAYS_BETWEEN_SNAPSHOTS))

        newest_local: datetime = self.time.toLocal(last_snapshot)
        time_that_day_local = datetime(newest_local.year, newest_local.month, newest_local.day, timeofDay[0], timeofDay[1], tzinfo=self.time.local_tz)
        if newest_local < time_that_day_local:
            # Latest snapshot is before the snapshot time for that day
            next = self.time.toUtc(time_that_day_local)
        else:
            # return the next snapshot after the delta
            next = self.time.toUtc(time_that_day_local + timedelta(days=self.config.get(Setting.DAYS_BETWEEN_SNAPSHOTS)))
        if next < now:
            return now
        else:
            return next

    def nextSnapshot(self, now: datetime):
        latest = max(self.snapshots.values(), default=None, key=lambda s: s.date())
        if latest:
            latest = latest.date()
        return self._nextSnapshot(now, latest)

    def sync(self, now: datetime):
        if self.simulate_error is not None:
            if self.simulate_error.startswith("test"):
                raise Exception(self.simulate_error)
            else:
                raise SimulatedError(self.simulate_error)
        self._syncSnapshots([self.source, self.dest])

        if self.dest.enabled():
            self._purge(self.source)
            self._purge(self.dest)

        next_snapshot = self.nextSnapshot(now)
        if next_snapshot and now >= next_snapshot and self.source.enabled() and self.dest.enabled():
            self.createSnapshot(CreateOptions(now, self.config.get(Setting.SNAPSHOT_NAME)))
            self._purge(self.source)

        if self.dest.enabled() and self.dest.upload():
            # get the snapshots we should upload
            uploads = []
            for snapshot in self.snapshots.values():
                if snapshot.getSource(self.source.name()) is not None and snapshot.getSource(self.source.name()).uploadable() and snapshot.getSource(self.dest.name()) is None:
                    uploads.append(snapshot)
            uploads.sort(key=lambda s: s.date())
            uploads.reverse()
            for upload in uploads:
                # only upload if doing so won't result in it being deleted next
                dummy = DummySnapshot("", upload.date(), self.dest.name(), "dummy_slug_name")
                proposed = list(self.snapshots.values())
                proposed.append(dummy)
                if self._nextPurge(self.dest, proposed) != dummy:
                    upload.addSource(self.dest.save(upload, self.source.read(upload)))
                    self._purge(self.dest)
                else:
                    break

    def createSnapshot(self, options):
        if not self.source.enabled():
            return
        created = self.source.create(options)
        snapshot = Snapshot(created)
        self.snapshots[snapshot.slug()] = snapshot

    def deleteSnapshot(self, snapshot, source):
        if not snapshot.getSource(source.name()):
            return
        slug = snapshot.slug()
        source.delete(snapshot)
        snapshot.removeSource(source.name())
        if snapshot.isDeleted():
            del self.snapshots[slug]

    def getNextPurges(self):
        purges = {}
        for source in [self.source, self.dest]:
            purges[source.name()] = self._nextPurge(source, self.snapshots.values(), findNext=True)
        return purges

    def _parseTimeOfDay(self) -> Optional[Tuple[int, int]]:
        from_config = self.config.get(Setting.SNAPSHOT_TIME_OF_DAY)
        if len(from_config) == 0:
            return None
        parts = from_config.split(":")
        if len(parts) != 2:
            return None
        try:
            hour: int = int(parts[0])
            minute: int = int(parts[1])
            if hour < 0 or minute < 0 or hour > 23 or minute > 59:
                return None
            return (hour, minute)
        except ValueError:
            # Parse error
            return None

    def _syncSnapshots(self, sources: List[SnapshotSource]):
        for source in sources:
            if source.enabled():
                from_source: Dict[str, AbstractSnapshot] = source.get()
            else:
                from_source: Dict[str, AbstractSnapshot] = {}
            for snapshot in from_source.values():
                if snapshot.slug() not in self.snapshots:
                    self.snapshots[snapshot.slug()] = Snapshot(snapshot)
                else:
                    self.snapshots[snapshot.slug()].addSource(snapshot)
            for snapshot in list(self.snapshots.values()):
                if snapshot.slug() not in from_source:
                    slug = snapshot.slug()
                    snapshot.removeSource(source.name())
                    if snapshot.isDeleted():
                        del self.snapshots[slug]
        self.firstSync = False

    def _nextPurge(self, source: SnapshotSource, snapshots, findNext=False):
        """
        Given a list of snapshots, decides if one should be purged.
        """
        count = source.maxCount()
        if findNext:
            count -= 1
        if source.maxCount() == 0 or not source.enabled() or len(snapshots) == 0:
            return None
        if self.generational_config:
            scheme = GenerationalScheme(self.time, self.generational_config, count=count)
        else:
            scheme = OldestScheme(count=count)
        consider_purging = []
        for snapshot in snapshots:
            source_snapshot = snapshot.getSource(source.name())
            if source_snapshot is not None and not source_snapshot.retained():
                consider_purging.append(snapshot)
        if len(consider_purging) == 0:
            return None
        return scheme.getOldest(consider_purging)

    def _purge(self, source: SnapshotSource):
        while True:
            purge = self._getPurgeList(source)
            if len(purge) <= 0:
                return
            if len(purge) > 1 and (self.config.get(Setting.CONFIRM_MULTIPLE_DELETES) and not self.info.isPermitMultipleDeletes()):
                raise DeleteMutlipleSnapshotsError(self._getPurgeStats())
            self.deleteSnapshot(purge[0], source)

    def _getPurgeStats(self):
        ret = {}
        for source in [self.source, self.dest]:
            ret[source.name()] = len(self._getPurgeList(source))
        return ret

    def _getPurgeList(self, source: SnapshotSource):
        if not source.enabled():
            return []
        candidates = list(self.snapshots.values())
        purges = []
        while True:
            next_purge = self._nextPurge(source, candidates)
            if next_purge is None:
                return purges
            else:
                purges.append(next_purge)
                candidates.remove(next_purge)
