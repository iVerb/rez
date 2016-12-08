from inspect import isclass
from hashlib import sha1
from abc import ABCMeta, abstractmethod

from rez.exceptions import ConfigurationError
from rez.vendor.version.version import _Comparable
from rez.utils.yaml import YamlDumpable


class ReverseSorted(_Comparable):
    """Used to add objects (ie, Version, VersionRange) to a sort key, but with
    reverse sorting
    """
    def __init__(self, obj):
        self.obj = obj

    def __eq__(self, other):
        return self.obj == other.obj

    def __lt__(self, other):
        if self.obj == other.obj:
            return False
        return not self.obj < other.obj

    def __repr__(self):
        return '%s(%r)' % (type(self).__name__, self.obj)


class PackageOrder(YamlDumpable):
    """Package reorderer base class."""
    __metaclass__ = ABCMeta

    name = None

    def __init__(self):
        pass

    def sort_key(self, package_name, version_like):
        """Returns a sort key usable for sorting these packages within the
        same family

        Args:
            package_name: (str) The family name of the package we are sorting
            verison_like: (Version|_LowerBound|_UpperBound|_Bound|VersionRange)
                the version-like object you wish to generate a key for

        Returns:
            Comparable object
        """
        from rez.vendor.version.version import Version, _LowerBound, _UpperBound, _Bound, VersionRange
        if isinstance(version_like, VersionRange):
            return tuple(self.sort_key(package_name, bound)
                         for bound in version_like.bounds)
        elif isinstance(version_like, _Bound):
            return (self.sort_key(package_name, version_like.lower),
                    self.sort_key(package_name, version_like.upper))
        elif isinstance(version_like, _LowerBound):
            inclusion_key = -2 if version_like.inclusive else -1
            return (self.sort_key(package_name, version_like.version),
                    inclusion_key)
        elif isinstance(version_like, _UpperBound):
            inclusion_key = 2 if version_like.inclusive else 1
            return (self.sort_key(package_name, version_like.version),
                    inclusion_key)
        elif isinstance(version_like, Version):
            # finally, the bit that we actually use the sort_key_implementation
            # for...
            return self.sort_key_implementation(package_name, version_like)

    @abstractmethod
    def sort_key_implementation(self, package_name, version):
        """Returns a sort key usable for sorting these packages within the
        same family

        Args:
            package_name: (str) The family name of the package we are sorting
            verison: (Version) the version object you wish to generate a key for

        Returns:
            Comparable object
        """
        raise NotImplementedError

    @abstractmethod
    def applies_to(self, package_name):
        """Returns whether or not this orderer should be used to reorder a
        package with the given family name

        Args:
            package_name: (str) The family name of the package we are considering

        Returns:
            (bool) Whether we should use this orderer to reorder the package
        """
        raise NotImplementedError

    @property
    def sha1(self):
        return sha1(repr(self)).hexdigest()

    def __repr__(self):
        return "%s.from_pod(%s)" % (self.__class__.__name__, repr(self.to_pod()))

    def __str__(self):
        return str(self.to_pod())

    def to_yaml_pod(self):
        data = self.to_pod()
        data['type'] = self.name
        return data


class DefaultAppliesOrder(PackageOrder):
    def init_packages(self, packages):
        if packages == "*":
            self.packages = packages
        else:
            if isinstance(packages, basestring):
                packages = [packages]
            self.packages = set(packages)

    def applies_to(self, package_name):
        """Returns whether or not this orderer should be used to reorder a
        package with the given family name

        The default implementation simply checks if a package name is in a list;
        or, if instead of list, the string "*" is passed, it matches all
        packages

        Args:
            package_name: (str) The family name of the package we are considering

        Returns:
            (bool) Whether we should use this orderer to reorder the package
        """
        if self.packages == "*":
            return True
        return package_name in self.packages


class ReversedPackageOrder(DefaultAppliesOrder):
    """Package orderer that sorts in reverse version order"""
    name = "reversed"

    def __init__(self, packages):
        """Create a reorderer.

        Args:
            packages: (str or list of str): packages that this orderer should apply to
        """
        self.init_packages(packages)

    def sort_key_implementation(self, package_name, version):
        return ReverseSorted(version)

    def to_pod(self):
        return dict(packages=sorted(self.packages))

    @classmethod
    def from_pod(cls, data):
        return cls(packages=data["packages"])


class TimestampPackageOrder(DefaultAppliesOrder):
    """A timestamp order function.

    Given a time T, this orderer returns packages released before T, in descending
    order, followed by those released after. If `rank` is non-zero, version
    changes at that rank and above are allowed over the timestamp.

    For example, consider the common case where we want to prioritize packages
    released before T, except for newer patches. Consider the following package
    versions, and time T:

        2.2.1
        2.2.0
        2.1.1
        2.1.0
        2.0.6
        2.0.5
              <-- T
        2.0.0
        1.9.0

    A timestamp orderer set to rank=3 (patch versions) will attempt to consume
    the packages in the following order:

        2.0.6
        2.0.5
        2.0.0
        1.9.0
        2.1.1
        2.1.0
        2.2.1
        2.2.0

    Notice that packages before T are preferred, followed by newer versions.
    Newer versions are consumed in ascending order, except within rank (this is
    why 2.1.1 is consumed before 2.1.0).
    """
    name = "soft_timestamp"

    def __init__(self, packages, timestamp, rank=0):
        """Create a reorderer.

        Args:
            packages: (str or list of str): packages that this orderer should apply to
            timestamp (int): Epoch time of timestamp. Packages before this time
                are preferred.
            rank (int): If non-zero, allow version changes at this rank or above
                past the timestamp.
        """
        self.init_packages(packages)
        self.timestamp = timestamp
        self.rank = rank

        # dictionary mapping from package family to the first-version-after
        # the given timestamp
        self._cached_first_after = {}
        self._cached_sort_key = {}

    def get_first_after(self, package_family):
        first_after = self._cached_first_after.get(package_family, KeyError)
        if first_after is KeyError:
            first_after = self._calc_first_after(package_family)
            self._cached_first_after[package_family] = first_after
        return first_after

    def _calc_first_after(self, package_family):
        from rez.packages_ import iter_packages
        descending = sorted(iter_packages(package_family),
                             key=lambda p: p.version,
                             reverse=True)

        first_after = None
        for i, package in enumerate(descending):
            if package.timestamp:
                if package.timestamp > self.timestamp:
                    first_after = package.version
                else:
                    if not self.rank:
                        return first_after
                    # if we have rank, then we need to then go back UP the
                    # versions, until we find one whose trimmed version doesn't
                    # match.
                    # Note that we COULD do this by simply iterating through
                    # an ascending sequence, in which case we wouldn't have to
                    # "switch direction" after finding the first result after
                    # by timestamp... but we're making the assumption that the
                    # timestamp break will be closer to the higher end of the
                    # version, and that we'll therefore have to check fewer
                    # timestamps this way...
                    trimmed_version = package.version.trim(self.rank - 1)
                    first_after = None
                    for after_package in reversed(descending[:i]):
                        if after_package.version.trim(self.rank - 1) != trimmed_version:
                            return after_package.version
                    return first_after
        return first_after

    def _calc_sort_key(self, package_name, version):
        first_after = self.get_first_after(package_name)
        if first_after is None:
            # all packages are before T
            is_before = True
        else:
            is_before = int(version < first_after)

        # ascend below rank, but descend within
        if is_before:
            return (is_before, version)
        else:
            if self.rank:
                return (is_before,
                        ReverseSorted(version.trim(self.rank - 1)),
                        version.tokens[self.rank - 1:])
            else:
                return (is_before, ReverseSorted(version))

    def sort_key_implementation(self, package_name, version):
        cache_key = (package_name, str(version))
        result = self._cached_sort_key.get(cache_key)
        if result is None:
            result = self._calc_sort_key(package_name, version)
            self._cached_sort_key[cache_key] = result
        return result

    def to_pod(self):
        return dict(packages=sorted(self.packages),
                    timestamp=self.timestamp,
                    rank=self.rank)

    @classmethod
    def from_pod(cls, data):
        return cls(packages=data["packages"],
                   timestamp=data["timestamp"],
                   rank=data.get("rank"))


class CustomPackageOrder(PackageOrder):
    """A package order that allows explicit specification of version ordering.

    Specified through the "packages" attributes, which should be a dict which
    maps from a package family name to a list of version ranges to prioritize,
    in decreasing priority order.

    As an example, consider a package splunge which has versions:

      [1.0, 1.1, 1.2, 1.4, 2.0, 2.1, 3.0, 3.2]

    By default, version priority is given to the higest version, so version
    priority, from most to least preferred, is:

      [3.2, 3.0, 2.1, 2.0, 1.4, 1.2, 1.1, 1.0]

    However, if you set a custom package order like this:

      package_orderers:
      - type: custom
        packages:
          splunge: ['2', '1.1+<1.4']

    Then the preferred versions, from most to least preferred, will be:
     [2.1, 2.0, 1.2, 1.1, 3.2, 3.0, 1.4, 1.0]

    Any version which does not match any of these expressions are sorted in
    decreasing version order (like normal) and then appended to this list (so they
    have lower priority). This provides an easy means to effectively set a
    "default version."  So if you do:

      package_orderers:
      - type: custom
        packages:
          splunge: ['3.0']

    resulting order is:

      [3.0, 3.2, 2.1, 2.0, 1.4, 1.2, 1.1, 1.0]

    You may also include a single False or empty string in the list, in which case
    all "other" versions will be placed at that spot. ie

      package_orderers:
      - type: custom
        packages:
          splunge: ['', '3+']

    yields:

     [2.1, 2.0, 1.4, 1.2, 1.1, 1.0, 3.2, 3.0]

    Note that you could also have gotten the same result by doing:

      package_orderers:
      - type: custom
        packages:
          splunge: ['<3']

    If a version matches more than one range expression, it will be placed at
    the highest-priority matching spot, so:

      package_orderers:
      - type: custom
        packages:
          splunge: ['1.2+<=2.0', '1.1+<3']

    gives:
     [2.0, 1.4, 1.2, 2.1, 1.1, 3.2, 3.0, 1.0]

    Also note that this does not change the version sort order for any purpose but
    determining solving priorities - for instance, even if version priorities is:

      package_orderers:
      - type: custom
        packages:
          splunge: [2, 3, 1]

    The expression splunge-1+<3 would still match version 2.
    """
    name = "custom"

    def __init__(self, packages):
        """Create a reorderer.

        Args:
            packages: (dict from str to list of VersionRange): packages that
                this orderer should apply to, and the version priority ordering
                for that package
        """
        self.packages = self._packages_from_pod(packages)
        self._version_key_cache = {}

    def sort_key_implementation(self, package_name, version):
        family_cache = self._version_key_cache.setdefault(package_name, {})
        key = family_cache.get(version)
        if key is not None:
            return key

        key = self.version_priority_key_uncached(package_name, version)
        family_cache[version] = key
        return key

    def version_priority_key_uncached(self, package_name, version):
        version_priorities = self.packages[package_name]

        default_key = -1
        for sort_order_index, range in enumerate(version_priorities):
            # in the config, version_priorities are given in decreasing
            # priority order... however, we want a sort key that sorts in the
            # same way that versions do - where higher values are higher
            # priority - so we need to take the inverse of the index
            priority_sort_key = len(version_priorities) - sort_order_index
            if range in (False, ""):
                if default_key != -1:
                    raise ValueError("version_priorities may only have one "
                                     "False / empty value")
                default_key = priority_sort_key
                continue
            if range.contains_version(version):
                break
        else:
            # For now, we're permissive with the version_sort_order - it may
            # contain ranges which match no actual versions, and if an actual
            # version matches no entry in the version_sort_order, it is simply
            # placed after other entries
            priority_sort_key = default_key
        return priority_sort_key, version

    def applies_to(self, package_name):
        return package_name in self.packages

    @classmethod
    def _packages_to_pod(cls, packages):
        return dict((package, [str(v) for v in versions])
                    for (package, versions) in packages.iteritems())

    @classmethod
    def _packages_from_pod(cls, packages):
        from rez.vendor.version.version import VersionRange
        parsed_dict = {}
        for package, versions in packages.iteritems():
            new_versions = []
            numFalse = 0
            for v in versions:
                if v in ("", False):
                    v = False
                    numFalse += 1
                else:
                    if not isinstance(v, VersionRange):
                        if isinstance(v, (int, float)):
                            v = str(v)
                        v = VersionRange(v)
                new_versions.append(v)
            if numFalse > 1:
                raise ConfigurationError("version_priorities for CustomPackageOrder may only have one False / empty value")
            parsed_dict[package] = new_versions
        return parsed_dict

    def to_pod(self):
        return dict(packages=self._packages_to_pod(self.packages))

    @classmethod
    def from_pod(cls, data):
        return cls(packages=data["packages"])


def to_pod(orderer):
    data_ = orderer.to_pod()
    data = (orderer.name, data_)
    return data


def from_pod(data):
    cls_name, data_ = data
    cls = _orderers[cls_name]
    return cls.from_pod(data_)


def register_orderer(cls):
    if isclass(cls) and issubclass(cls, PackageOrder) and \
            hasattr(cls, "name") and cls.name:
        _orderers[cls.name] = cls
        return True
    else:
        return False

def get_orderer(package_name, orderers=None):
    from rez.config import config
    if orderers is None:
        orderers = config.package_orderers
    if not orderers:
        orderers = []
    found_orderer = None
    for orderer in orderers:
        if orderer.applies_to(package_name):
            found_orderer = orderer
            break
    return found_orderer

# registration of builtin orderers
_orderers = {}
for o in globals().values():
    register_orderer(o)


# Copyright 2013-2016 Allan Johns.
#
# This library is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.
