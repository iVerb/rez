"""
rez packages
"""
import os.path
import re
from public_enums import PKG_METADATA_FILENAME
import rez_metafile
import rez_filesys
from rez_exceptions import PkgSystemError
from versions import Version, ExactVersion

PACKAGE_NAME_REGSTR = '[a-zA-Z][a-zA-Z0-9_]*'
PACKAGE_NAME_REGEX = re.compile(PACKAGE_NAME_REGSTR + '$')

def split_name(pkg_str):
    strs = pkg_str.split('-')
    if len(strs) > 2:
        PkgSystemError("Invalid package string '" + pkg_str + "'")
    name = strs[0]
    if len(strs) == 1:
        verrange = ""
    else:
        verrange = strs[1]
    return name, verrange

def pkg_name(pkg_str):
    return split_name(pkg_str)[0]

# TODO: move this to RezMemCache
def get_family_paths(path):
    return [(x, os.path.join(path, x)) for x in os.listdir(path) \
            if not PACKAGE_NAME_REGEX.match(os.path.basename(x)) and x not in ['rez']]

def iter_package_families(name=None, paths=None):
    """
    Iterate through top-level `PackageFamily` instances.
    """
    if paths is None:
        paths = rez_filesys._g_syspaths
    elif isinstance(paths, basestring):
        paths = [paths]

    for pkg_path in paths:
        if name is not None:
            family_path = os.path.join(pkg_path, name)
            if os.path.isdir(family_path):
                yield PackageFamily(name, family_path)
            elif os.path.isfile(family_path + '.yaml'):
                yield ExternalPackageFamily(name, family_path + '.yaml')
        else:
            for family_name, family_path in get_family_paths(pkg_path):
                if family_path.endswith('.yaml'):
                    yield ExternalPackageFamily(family_name, family_path)
                else:
                    yield PackageFamily(family_name, family_path)

def package_family(name, paths=None):
    """
    Return the first `FamilyPackage` found on the search path.
    """
    result = iter_package_families(name, paths)
    try:
        # return first item in generator
        return next(result)
    except StopIteration:
        return None

def iter_packages(name=None, paths=None, skip_dupes=True):
    """
    Iterate through all packages
    """
    done = set()
    for pkg_fam in iter_package_families(name, paths):
        for pkg in pkg_fam.iter_version_packages():
            if skip_dupes:
                if pkg.version not in done:
                    done.add(pkg.version)
                    yield pkg
            else:
                yield pkg

def iter_packages_in_range(family_name, ver_range, latest=True, timestamp=0,
                           exact=False, paths=None):
    """
    Given a family name and a `VersionRange`, iterate over `Package` instances.

    If two versions in two different paths are the same, then the package in
    the first path is returned in preference.
    """
    # store the generator. no paths have been walked yet
    results = iter_packages(family_name, paths)

    if timestamp:
        results = [x for x in results if x.timestamp <= timestamp]
    # sort
    if latest:
        results = sorted(results, key=lambda x: x.version, reverse=True)
    else:
        results = sorted(results, key=lambda x: x.version, reverse=False)

    # find the best match, skipping dupes
    for result in results:
        if ver_range.matches_version(result.version, allow_inexact=not exact):
            yield result

def package_in_range(family_name, ver_range, latest=True, timestamp=0,
                     exact=False, paths=None):
    """
    Return the first `Package` found on the search path.
    """
    result = iter_packages_in_range(family_name, ver_range, latest, timestamp,
                                    exact, paths)
    try:
        # return first item in generator
        return next(result)
    except StopIteration:
        return None

class PackageFamily(object):
    """
    A package family contains versioned packages of the same name.

    A package family has a single root directory, and there may be multiple
    package families on the rez search path with the same name.
    """
    def __init__(self, name, path):
        self.name = name
        self.path = path

    def iter_version_packages(self):
        from rez_memcached import get_memcache
        metafile = os.path.join(self.path, PKG_METADATA_FILENAME)
        if os.path.isfile(metafile):
            # check for special case - unversioned package.
            # only allowed when no versioned packages exist.
            yield Package(self.name, Version(""), metafile, 0)
        else:
            vers = get_memcache().get_versions_in_directory(self.path)
            if vers:
                for ver, timestamp in vers:
                    metafile = os.path.join(self.path, str(ver), PKG_METADATA_FILENAME)
                    # no need to check if metafile is exists, already done in `get_versions_in_directory`
                    yield Package(self.name, ver, metafile, timestamp)

#     def version_package(self, ver_range, latest=True, exact=False, timestamp=0):
#         """
#         Given a a `VersionRange`, return a `Package` instance, or None if no
#         matches are found.
#         """
#         # store the generator. no paths have been walked yet
#         results = self.iter_version_packages()
# 
#         if timestamp:
#             results = [x for x in results if x.timestamp <= timestamp]
#         # sort 
#         if latest:
#             results = sorted(results, key=lambda x: x.version, reverse=True)
#         else:
#             results = sorted(results, key=lambda x: x.version, reverse=False)
# 
#         # find the best match
#         for result in results:
#             if ver_range.matches_version(result.version, allow_inexact=not exact):
#                 return result
#         return None

class ExternalPackageFamily(PackageFamily):
    """
    special case where the entire package is stored in one file
    """
    def __init__(self, name, path):
        PackageFamily.__init__(self, name, path)
        self._metadata = None

    @property
    def raw_metadata(self):
        # bypass the memcache so that non-essentials are not stripped
        if self._metadata is None:
            self._metadata = rez_metafile.load_metadata(self.path)
            if isinstance(self._metadata, list):
                family_data = self._metadata[0]
                # copy data from main metadata into sub-sections
                for ver_data in self._metadata[1:]:
                    # only set data from family package that does not exist in version package
                    for key, value in family_data.iteritems():
                        ver_data.setdefault(key, value)
        return self._metadata

    @property
    def metadata(self):
        if isinstance(self.raw_metadata, list):
            return self.raw_metadata[0]
        else:
            return self.raw_metadata

    @property
    def sub_metadata(self):
        if isinstance(self.raw_metadata, list):
            return self.raw_metadata[1:]
        else:
            return []

    @property
    def versions(self):
        print self.name, sorted(self.metadata.versions)
        return [ExactVersion(x) for x in sorted(self.metadata.versions)]

    def iter_version_packages(self):
        for version in self.versions:
            data = self.metadata
            for ver_data in self.sub_metadata:
                if version in Version(ver_data.version):
                    data = ver_data.copy()
                    break
            data['version'] = version
            # this directory does not exist, but it's still useful in output and errors
            path = os.path.splitext(self.path)[0]
            path = os.path.join(path, str(data['version']))
            yield Package(self.name, version,
                          path,
                          0,
                          metadata=data,
                          stripped_metadata=data)

class Package(object):
    """
    an unresolved package.

    An unresolved package has a version, but may have inexplicit or "variant"
    requirements, which can only be determined once its co-packages
    are known. When the exact list of requirements is determined, the package
    is considered resolved and the full path to the package root is known.
    """
    def __init__(self, name, version, path, timestamp, metadata=None,
                 stripped_metadata=None):
        self.name = name
        self.version = version
        # for convenience, base may be a path or a metafile
        if path.endswith('.yaml'):
            self.base = os.path.dirname(path)
            self.metafile = path
        else:
            self.base = path
            self.metafile = os.path.join(self.base, PKG_METADATA_FILENAME)
        self.timestamp = timestamp
        self._metadata = metadata
        self._stripped_metdata = stripped_metadata

    @property
    def metadata(self):
        # bypass the memcache so that non-essentials are not stripped
        if self._metadata is None:
            self._metadata = rez_metafile.load_metadata(self.metafile)
        return self._metadata

    @property
    def stripped_metadata(self):
        """
        only the essential metadata
        """
        if self._stripped_metdata is None:
            from rez_memcached import get_memcache
            self._stripped_metdata = get_memcache().get_metafile(self.metafile)
        return self._stripped_metdata

    def short_name(self):
        if (len(str(self.version)) == 0):
            return self.name
        else:
            return self.name + '-' + str(self.version)

    def __str__(self):
        return str([self.name, self.version, self.base])

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.name,
                               self.version)

class ResolvedPackage(Package):
    """
    A resolved package.

    An unresolved package has a version, but may have inexplicit or "variant"
    requirements, which can only be determined once its co-packages
    are known. When the exact list of requirements is determined, the package
    is considered resolved and the full path to the package root is known.
    """
    def __init__(self, name, version, base, root, commands, metadata, timestamp):
        Package.__init__(self, name, version, base, timestamp, stripped_metadata=metadata)
        # FIXME: this is primarily here for rex. i don't like the fact that
        # Package.version is a Version, and ResolvedPackage.version is a ExactVersion.
        # look into moving functionality of ExactVersion onto Version
        if version:
            self.version = ExactVersion(version)
        else:
            self.version = ''
        self.root = root
        self.raw_commands = commands
        self.commands = None

    def strip(self):
        # remove data that we don't want to cache
        self.commands = None
        self.raw_commands = None
        self._metadata = None

    def __str__(self):
        return str([self.name, self.version, self.root])

    def __repr__(self):
        return "%s(%r, %r, %r)" % (self.__class__.__name__, self.name,
                                   self.version, self.root)
