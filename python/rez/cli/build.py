"""
Tool for configuring and running cmake for the project in the current directory, and possibly make (or equivalent).

A 'package.yaml' file must exist in the current directory, and this drives the build. This utility is designed so
that it is possible to either automatically build an entire matrix for a given project, or to perform the build for
each variant manually. Note that in the following descriptions, 'make' is used to mean either make or equivalent.
The different usage scenarios are described below.

Usage case 1:

    rez-build [-v <variant_num>] [-m earliest|latest(default)] [-- cmake args]

This will use the package.yaml to spawn the correct shell for each variant, and create a 'build-env.sh' script (named
'build-env.0.sh,..., build-env.N.sh for each variant). Invoking one of these scripts will spawn an environment and
automatically run cmake with the supplied arguments - you can then execute make within this context, and this will
build the given variant. If zero or one variant exists, then 'build-env.sh' is generated.

Usage case 2:

    rez-build [[-v <variant_num>] [-n]] [-m earliest|latest(default)] -- [cmake args] -- [make args]

This will use the package.yaml to spawn the correct shell for each variant, invoke cmake, and then invoke make (or
equivalent). Use rez-build in this way to automatically build the whole build matrix. rez-build does a 'make clean'
before makeing by default, but the '-n' option suppresses this. Use -n in situations where you build a specific variant,
and then want to install that variant without rebuilding everything again. This option is only available when one
variant is being built, otherwise we run the risk of installing code that has been built for a different variant.

Examples of use:

Generate 'build-env.#.sh' files and invoke cmake for each variant, but do not invoke make:

    rez-build --

Builds all variants of the project, spawning the correct shell for each, and invoking make for each:

    rez-build -- --

Builds only the first variant of the project, spawning the correct shell, and invoking make:

    rez-build -v 0 -- --

Generate 'build-env.0.sh' and invoke cmake for the first (zeroeth) variant:

    rez-build -v 0 --

or:

    rez-build -v 0

Build the second variant only, and then install it, avoiding a rebuild:

    rez-build -v 1 -- --
    rez-build -v 1 -n -- -- install

"""

# FIXME: need to use raw help for text above

import sys
import os
import stat
import inspect
import traceback
import os.path
import shutil
import subprocess
import argparse
import textwrap
import abc
from rez.cli import error, output

BUILD_SYSTEMS = {'eclipse' : "Eclipse CDT4 - Unix Makefiles",
                 'codeblocks' : "CodeBlocks - Unix Makefiles",
                 'make' : "Unix Makefiles",
                 'xcode' : "Xcode"}

# in python 2.7, this list is stored in hashlib.algorithms
HASH_TYPES = ('md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512')
#
#-#################################################################################################
# usage/parse args
#-#################################################################################################

# usage(){
#     /bin/cat $0 | grep '^##' | sed 's/^## //g' | sed 's/^##//g'
#     sys.exit(1)
# }
# 
# [[ $# == 0 ]] && usage
# [[ "$1" == "-h" ]] && usage
# 
# 
# # gather rez-build args
# ARGS1=
# 
# 
# while [ $# -gt 0 ]; do
#     if [ "$1" == "--" ]:
#         shift
#         break
#     fi
#     ARGS1=$ARGS1" "$1
#     shift
# done
# 
# if [ "$ARGS1" != "" ]:
#     while getopts iudgm:v:ns:c:t: OPT $ARGS1 ; do
#         case "$OPT" in
#             m)    opts.mode=$OPTARG
#                 ;;
#             v)    opts.variant_nums=$OPTARG
#                 ;;
#             n)    opts.no_clean=1
#                 ;;
#             s)    opts.vcs_metadata=$OPTARG
#                 ;;
#             c)  opts.changelog=$OPTARG
#                 ;;
#             t)    opts.time=$OPTARG
#                 ;;
#             i)    opts.print_install_path=1
#                 ;;
#             u)    opts.ignore_blacklist='--ignore-blacklist'
#                 ;;
#             g)    opts.no_archive='--ignore-archiving'
#                 ;;
#             d)    opts.no_assume_dt='--no-assume-dt'
#                 ;;
#             *)    sys.exit(1)
#                 ;;
#         esac
#     done
# fi

def unversioned(pkgname):
    return pkgname.split('-')[0]

def _get_package_metadata(filepath, quiet=False, no_catch=False):
    from rez.rez_metafile import ConfigMetadata
    # load yaml
    if no_catch:
        metadata = ConfigMetadata(filepath)
    else:
        try:
            metadata = ConfigMetadata(filepath)
        except Exception as e:
            if not quiet:
                error("Malformed package.yaml: '" + filepath + "'." + str(e))
            sys.exit(1)

    if not metadata.version:
        if not quiet:
            error("No 'metadata.version' in " + filepath + ".\n")
        sys.exit(1)

    if metadata.name:
        # FIXME: this should be handled by ConfigMetadata class
        bad_chars = [ '-', '.' ]
        for ch in bad_chars:
            if (metadata.name.find(ch) != -1):
                error("Package name '" + metadata.name + "' contains illegal character '" + ch + "'.")
                sys.exit(1)
    else:
        if not quiet:
            error("No 'name' in " + filepath + ".")
        sys.exit(1)
    return metadata

def _get_variants(metadata, variant_nums):
    all_variants = metadata.get_variants()
    if all_variants:
        if variant_nums:
            variants = []
            for variant_num in variant_nums:
                try:
                    variants.append((variant_num, all_variants[variant_num]))
                except IndexError:
                    error("Variant #" + str(variant_num) + " does not exist in package.")
            return variants
        else:
            # get all variants
            return [(i, var) for i, var in enumerate(all_variants)]
    else:
        return [(-1, None)]


class SourceRetrieverError(Exception):
    pass

class SourceRetriever(object):
    '''Classes which are used to retrieve source necessary for building.

    The use of these classes is triggered by the inclusion of url entries
    in the external_build dict of the package.yaml file
    '''
    __metaclass__ = abc.ABCMeta

    SOURCE_DIR = 'src'

    # override with a list of names that must be in the url's metadata dict
    REQUIRED_METADATA = ['url']

    # override with a name for this type of SourceRetriever, for use in
    # package.yaml files
    TYPE_NAME = None

    def __init__(self, metadata):
        '''Construct a SourceRetriever object from the given (raw) metadata dict
        (ie, as parsed straight from the yaml file).  Will raise a
        SourceRetrieverMissingMetadataError if the metadata is not compatible
        with this SourceRetriever
        '''
        self.metadata = self.parse_metadata(metadata)

    @property
    def url(self):
        return self.metadata['url']

    @classmethod
    def parse_metadata(cls, raw_metadata):
        parsed = dict(raw_metadata)
        for required_attr in cls.REQUIRED_METADATA:
            if required_attr not in parsed:
                raise SourceRetrieverError('%s classes must define %s in their'
                                           ' metadata' % (cls.__name__,
                                                          required_attr))
        return parsed

    @abc.abstractmethod
    def get_source(self):
        raise NotImplementedError

    @classmethod
    def get_source_retrievers(cls, metadata):
        '''Given a metadata object, returns SourceRetriever objects for all the
        url entries in the external_build section
        '''
        retrievers = []
        build_data = metadata.metadict.get('external_build')
        if build_data:
            url = cls._get_url(build_data)
            if url:
                urls = [url]
            else:
                urls = [cls._get_url(x) for x in build_data.get('urls', [])]
            if urls:
                for url, retriever_class, metadata in urls:
                    retrievers.append(retriever_class(metadata))
        return retrievers

    @classmethod
    def _get_url(cls, metadict):
        """
        Return the (url, retriever_class, metadict) for the given metadict or
        None, if no url entry is present
        """
        url = metadict.get('url')
        if not url:
            return None

        # TODO: more gud smart make logic for figuring out type from url!
        type_name = metadict.get('type')
        if not type_name:
            basename = url.rsplit('/', 1)[-1]
            ext = os.path.splitext(basename)[-1]
            ext_to_type = {
                '.gz': 'archive',  # also covers .tar.gz
                '.tar': 'archive',
                # '.zip': 'archive', # haven't implemented yet
                '.git': 'git',
                '.hg': 'hg',
            }
            type_name = ext_to_type.get(ext, SourceDownloader)
        return url, cls.type_name_to_class(type_name), metadict

    TYPE_NAME_TO_CLASS = None

    @classmethod
    def type_name_to_class(cls, type_name):
        if cls.TYPE_NAME_TO_CLASS is None:
            # populate TYPE_NAME_TO_CLASS if we haven't yet
            cls.TYPE_NAME_TO_CLASS = {}

            def is_retriever(obj):
                return (inspect.isclass(obj)
                        and issubclass(obj, SourceRetriever)
                        # used to do:
                        #and not inspect.isabstract(obj)
                        # ...but technically, RepoCloner isn't abstract, because
                        # all of it's "overridden" methods are classmethods,
                        # which as of python 2.7 can't be made abstract...
                        and obj.TYPE_NAME is not None)

            for obj in globals().itervalues():
                if is_retriever(obj):
                    curr_name = obj.TYPE_NAME
                    if curr_name != curr_name.lower():
                        raise ValueError("Invalid TYPE_NAME %r for %s - must be"
                                         " all lower case" % (curr_name,
                                                              obj.__name__))
                    existing_cls = cls.TYPE_NAME_TO_CLASS.get(curr_name)
                    if existing_cls:
                        raise ValueError("Duplicate TYPE_NAME %r (%s and %s)"
                                         % (curr_name, obj.__name__,
                                            existing_cls.__name__))
                    cls.TYPE_NAME_TO_CLASS[curr_name] = obj
        try:
            return cls.TYPE_NAME_TO_CLASS[type_name]
        except KeyError:
            raise SourceRetrieverError("unrecognized SourceRetriever type name"
                                       " %r - valid values are %s"
                                       % (type_name,
                                          ', '.join(cls.TYPE_NAME_TO_CLASS.itervalues())))


class SourceDownloader(SourceRetriever):
    TYPE_NAME = 'archive'

    @property
    def hash_str(self):
        return self.metadata['hash_str']

    @property
    def hash_type(self):
        return self.metadata['hash_type']

    @classmethod
    def parse_metadata(cls, raw_metadata):
        # get the hash string and hash type
        metadata = super(SourceDownloader, cls).parse_metadata(raw_metadata)
        url = metadata['url']
        for hash_type in HASH_TYPES:
            hash_str = metadata.get(hash_type)
            if hash_str:
                metadata['hash_str'] = hash_str
                metadata['hash_type'] = hash_type
                return metadata
        raise SourceRetrieverError("when providing a download url for"
            " external build you must also provide a checksum entry (%s):"
            " %s" % (', '.join(HASH_TYPES), url))

    @classmethod
    def _download(cls, url, file_name):
        import urllib2

        u = urllib2.urlopen(url)

        with open(file_name, 'wb') as f:
            meta = u.info()
            file_size = int(meta.getheaders("Content-Length")[0])
            print "Downloading: %s Bytes: %s" % (file_name, file_size)

            file_size_dl = 0
            block_sz = 8192
            while True:
                buffer = u.read(block_sz)
                if not buffer:
                    break

                file_size_dl += len(buffer)
                f.write(buffer)
                status = r"%10d  [%3.2f%%]" % (file_size_dl, file_size_dl * 100. / file_size)
                status = status + chr(8)*(len(status)+1)
                print status,

    @classmethod
    def _source_archive_path(cls, url):
        """
        get the path for the local source archive
        """
        from urlparse import urlparse
        import posixpath
        url_parts = urlparse(url)
        archive = posixpath.basename(url_parts.path)
        archive_dir = os.environ.get('REZ_BUILD_DOWNLOAD_CACHE', '.rez-downloads')
        if not os.path.isdir(archive_dir):
            os.makedirs(archive_dir)
        return os.path.join(archive_dir, archive)

    @classmethod
    def _extract_tar(cls, tarpath):
        """
        extract the tar file at the given path, returning the common prefix of all
        paths in the archive
        """
        import tarfile
        print "extracting %s" % tarpath
        tar = tarfile.open(tarpath)
        try:
            prefix = os.path.commonprefix(tar.getnames())
            srcdir = 'src'
            tar.extractall(srcdir)
            return os.path.join(srcdir, prefix)
        finally:
            tar.close()
            print "done"

    @classmethod
    def _check_hash(cls, source_path, checksum, hash_type):
        import hashlib
        hasher = hashlib.new(hash_type)
        with open(source_path, 'rb') as f:
            while True:
                # read in 16mb blocks
                buf = f.read(16 * 1024 * 1024)
                if not buf:
                    break
                hasher.update(buf)
        real_checksum = hasher.hexdigest()
        if checksum != real_checksum:
            error("checksum mismatch: expected %s, got %s" % (real_checksum, checksum))
            sys.exit(1)

    def get_source(self):
        """
        Download and extract the source at the given url, caching it for reuse.

        Returns the common prefix of all folders in the source archive, or None
        if the download was unsuccessful.
        """
        source_path = self._source_archive_path(self.url)
        if not os.path.isfile(source_path):
            try:
                self._download(self.url, source_path)
            except Exception as e:
                err_msg = ''.join(traceback.format_exception_only(type(e), e))
                print "error downloading %s: %s" % (self.url, err_msg.rstrip())
                return
        else:
            print "Using cached archive %s" % source_path
        self._check_hash(source_path, self.hash_str, self.hash_type)
        # TODO: option not to re-extract?
        # TODO: support for other compression types
        return self._extract_tar(source_path)

class RepoCloner(SourceRetriever):
    REQUIRED_METADATA = SourceRetriever.REQUIRED_METADATA + ['revision']

    @classmethod
    def _subprocess(cls, args, wait=True, check_return=True,
                    **subprocess_kwargs):
        '''Run a git command for the given repo_dir, with the given args

        Parameters
        ----------
        args : strings
            args to pass to subprocess.call (or subprocess.Popen, if wait is
            False)
        wait : if True, then the result of subprocess.call is returned (ie,
            we wait for the process to finish, and return the returncode); if
            False, then the result of subprocess.Popen is returned (ie, we do
            not wait for the process to finish, and return the Popen object)
        check_return:
            if wait is True, and check_return is True, then an error will be
            raised if the return code is non-zero
        subprocess_kwargs : strings
            keyword args to pass to subprocess.call (or subprocess.Popen, if
            wait is False)
        '''
        if wait:
            exitcode = subprocess.call(args, **subprocess_kwargs)
            if check_return and exitcode:
                raise RuntimeError("Error running %r - exitcode: %d"
                                   % (' '.join(args), exitcode))
            return exitcode
        else:
            return subprocess.Popen(args, **subprocess_kwargs)

    @property
    def revision(self):
        return self.metadata['revision']

    @classmethod
    def repo_has_revision(cls, repo_dir, revision):
        raise NotImplementedError

    @classmethod
    def repo_clone(cls, repo_dir, repo_url):
        raise NotImplementedError

    @classmethod
    def repo_pull(cls, repo_dir, repo_url):
        raise NotImplementedError

    @classmethod
    def repo_update(cls, repo_dir, revision):
        raise NotImplementedError

    def get_source(self):
        repo_dir = self.SOURCE_DIR
        if not os.path.isdir(repo_dir):
            print "Cloning repo %s (to %s)" % (self.url, repo_dir)
            self.repo_clone(repo_dir, self.url)
        elif not self.repo_has_revision(repo_dir, self.revision):
            print "Pulling from repo %s (to %s)" % (self.url, repo_dir)
            self.repo_pull(repo_dir, self.url)

        print "Updating repo %s to %s" % (repo_dir, self.revision)
        self.repo_update(repo_dir, self.revision)
        return repo_dir


class GitCloner(RepoCloner):
    TYPE_NAME = 'git'

    @classmethod
    def git(cls, repo_dir, git_args, wait=True, check_return=True,
            **subprocess_kwargs):
        '''Run a git command for the given repo_dir, with the given args

        Parameters
        ----------
        repo_dir : basestring or None
            if non-None, a git working dir to set as the repo to use; note that
            since this is a required argument, if you wish to run a git command
            that does not need a current repository (ie,
            'git --version', 'hg clone', etc), you must explicitly pass None
        git_args : strings
            args to pass to git (as on the command line)
        wait : if True, then the result of subprocess.call is returned (ie,
            we wait for the process to finish, and return the returncode); if
            False, then the result of subprocess.Popen is returned (ie, we do
            not wait for the process to finish, and return the Popen object)
        check_return:
            if wait is True, and check_return is True, then an error will be
            raised if the return code is non-zero
        subprocess_kwargs : strings
            keyword args to pass to subprocess.call (or subprocess.Popen, if
            wait is False)
        '''
        args = ['git']
        if repo_dir is not None:
            args.extend(['--work-tree', repo_dir, '--git-dir',
                         os.path.join(repo_dir, '.git')])
        args.extend(git_args)
        return cls._subprocess(args, wait=wait, check_return=check_return,
                               **subprocess_kwargs)

    @classmethod
    def _current_branch(cls, repo_dir):
        #proc = cls.git(repo_dir, ['branch'], wait=False, stdout=subprocess.PIPE)
        proc = cls.git(repo_dir, ['rev-parse', '--abbrev-ref', 'HEAD'],
                       wait=False, stdout=subprocess.PIPE)
        stdout = proc.communicate()[0]
        if proc.returncode:
            raise RuntimeError("Error running git rev-parse - exitcode: %d"
                               % proc.returncode)
        return stdout.strip()

    @classmethod
    def _repo_remote_for_url(cls, repo_dir, repo_url):
        '''Given a remote repo url, returns the remote name that has that url
        as it's fetch url (creating / setting the rez_remote remote, if none
        exists)
        '''
        default_remote = 'rez_remote'

        proc = cls.git(repo_dir, ['remote', '-v'], wait=False,
                       stdout=subprocess.PIPE)
        stdout = proc.communicate()[0]
        if proc.returncode:
            raise RuntimeError("Error running git branch - exitcode: %d"
                               % proc.returncode)

        # for comparison, we need to "standardize" the repo url, by removing
        # any multiple whitespace (though there probably shouldn't be
        # whitespace)
        repo_url = ' '.join(repo_url.strip().split())

        found_default = False
        for line in stdout.split('\n'):
            # line we want looks like:
            # origin  git@github.com:SomeGuy/myrepo.git (fetch)
            fetch_str = ' (fetch)'
            line = line.strip()
            if not line.endswith(fetch_str):
                continue
            split_line = line[:-len(fetch_str)].split()
            if len(split_line) < 2:
                continue
            remote_name = split_line[0]
            if remote_name == default_remote:
                found_default = True
            remote_url = ' '.join(split_line[1:])
            print "remote_url: %r" % remote_url
            if remote_url == repo_url:
                return remote_name

        # if we've gotten here, we didn't find an existing remote that had
        # the desired url...

        if not found_default:
            # make one...
            cls.git(repo_dir, ['remote', 'add', default_remote, repo_url])
        else:
            # ...or update existing...
            cls.git(repo_dir, ['remote', 'set-url', default_remote, repo_url])
        return default_remote

    @classmethod
    def repo_has_revision(cls, repo_dir, revision):
        exitcode = cls.git(repo_dir, ['cat-file', '-e', revision],
                           check_return=False)
        return exitcode == 0

    @classmethod
    def repo_clone(cls, repo_dir, repo_url):
        # -n makes it not do a checkout
        cls.git(None, ['clone', '-n', repo_url, repo_dir])

    @classmethod
    def repo_pull(cls, repo_dir, repo_url):
        remote_name = cls._repo_remote_for_url(repo_dir, repo_url)
        cls.git(repo_dir, ['fetch', remote_name])

    @classmethod
    def repo_update(cls, repo_dir, revision):
        branch = cls._current_branch(repo_dir)
        # need to use different methods to update, depending on whether or
        # not we're switching branches...
        if branch == 'rez':
            # if branch is already rez, need to use "reset"
            cls.git(repo_dir, ['reset', '--hard', revision])
        else:
            # create / checkout a branch called "rez"
            cls.git(repo_dir, ['checkout', '-B', 'rez', revision])

class HgCloner(RepoCloner):
    TYPE_NAME = 'hg'

    @classmethod
    def hg(cls, repo_dir, hg_args, wait=True, check_return=True,
           **subprocess_kwargs):
        '''Run an hg command for the given repo_dir, with the given args

        Parameters
        ----------
        repo_dir : basestring or None
            if non-None, a mercurial working dir to set as the repo to use; note
            that since this is a required argument, if you wish to run an hg
            command that does not need a current repository (ie, 'hg --version',
            'hg clone', etc), you must explicitly pass None
        hg_args : strings
            args to pass to hg (as on the command line)
        wait : if True, then the result of subprocess.call is returned (ie,
            we wait for the process to finish, and return the returncode); if
            False, then the result of subprocess.Popen is returned (ie, we do
            not wait for the process to finish, and return the Popen object)
        check_return:
            if wait is True, and check_return is True, then an error will be
            raised if the return code is non-zero
        subprocess_kwargs : strings
            keyword args to pass to subprocess.call
        '''
        args = ['hg']
        if repo_dir is not None:
            args.extend(['-R', repo_dir])
        args.extend(hg_args)
        return cls._subprocess(args, wait=wait, check_return=check_return,
                               **subprocess_kwargs)

    @classmethod
    def repo_has_revision(cls, repo_dir, revision):
        # don't want to print error output if revision doesn't exist, so
        # use subprocess.PIPE to swallow output
        exitcode = cls.hg(repo_dir, ['id', '-r', revision], check_return=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return exitcode == 0

    @classmethod
    def repo_clone(cls, repo_dir, repo_url):
        cls.hg(None, ['clone', '--noupdate', repo_url, repo_dir])

    @classmethod
    def repo_pull(cls, repo_dir, repo_url):
        cls.hg(repo_dir, ['pull', repo_url])

    @classmethod
    def repo_update(cls, repo_dir, revision):
        cls.hg(repo_dir, ['update', revision])


def _write_cmakelist(install_commands, srcdir, working_dir_mode):
    assert not os.path.isabs(srcdir)
    srcroot = os.path.split(srcdir)[0]
    # there are different modes available for the current working directory
    working_dir_mode = working_dir_mode.lower()
    if working_dir_mode == 'source':
        working_dir = "${REZ_SOURCE_DIR}"
    elif working_dir_mode == 'source_root':
        working_dir = "${REZ_SOURCE_ROOT}" 
    elif working_dir_mode == 'build':
        working_dir = "${CMAKE_BINARY_DIR}"
    else:
        error("Invalid option for 'working_dir': provide one of 'source', 'source_root', or 'build'")
        sys.exit(1)

    lines = ['custom_build ALL ' + install_commands[0]]
    for line in install_commands[1:]:
        if line.strip():
            lines.append('  COMMAND ' + line)

    import re
    extra_cmake_commands = []
    for line in install_commands:
        for cmake_var in re.findall('\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}', line):
            extra_cmake_commands.append('message("%s = ${%s}")' % (cmake_var, cmake_var))
    if extra_cmake_commands:
        extra_cmake_commands.insert(0, 'message("")')
        extra_cmake_commands.append('message("")')

    text = """\
CMAKE_MINIMUM_REQUIRED(VERSION 2.8)

include(RezBuild)

rez_find_packages(PREFIX pkgs AUTO)

# TODO: create a cmake variable for the extracted source directory and temp install directory
  
# trailing slash tells install to copy the directory contents
set(REZ_INSTALL_DIR ${CMAKE_BINARY_DIR}/${REZ_BUILD_PROJECT_NAME}/)

install( DIRECTORY ${REZ_INSTALL_DIR}
  DESTINATION .
)

set(REZ_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/%s)
set(REZ_SOURCE_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/%s)

%s

add_custom_target(
  %s
  WORKING_DIRECTORY %s
)

# Create Cmake file
rez_install_cmake(AUTO)""" % (srcdir,
                              srcroot,
                              '\n'.join(extra_cmake_commands),
                              '\n'.join(lines),
                              working_dir)

    with open('CMakeLists.txt', 'w') as f:
        f.write(text)

def _format_bash_command(args):
    def quote(arg):
        if ' ' in arg:
            return "'%s'" % arg
        return arg
    cmd = ' '.join([quote(arg) for arg in args ])
    return textwrap.dedent("""
        echo
        echo rez-build: calling \\'%(cmd)s\\'
        %(cmd)s
        if [ $? -ne 0 ]; then
            exit 1 ;
        fi
        """ % {'cmd' : cmd})

def get_cmake_args(build_system, build_target, release=False):
    cmake_arguments = ["-DCMAKE_SKIP_RPATH=1"]

    # Rez custom module location
    cmake_arguments.append("-DCMAKE_MODULE_PATH=$CMAKE_MODULE_PATH")

    # Fetch the initial cache if it's defined
    if 'CMAKE_INITIAL_CACHE' in os.environ:
        cmake_arguments.extend(["-C", "$CMAKE_INITIAL_CACHE"])

    cmake_arguments.extend(["-G", build_system])

    cmake_arguments.append("-DCMAKE_BUILD_TYPE=%s" % build_target)

    if release:
        if os.environ.get('REZ_IN_REZ_RELEASE') != "1":
            result = raw_input("You are attempting to install centrally outside "
                               "of rez-release: do you really want to do this (y/n)? ")
            if result != "y":
                sys.exit(1)
        cmake_arguments.append("-DCENTRAL=1")

    return cmake_arguments

def _chmod(path, mode):
    if stat.S_IMODE(os.stat(path).st_mode) != mode:
        os.chmod(path, mode)

# def foo():
#     if print_build_requires:
#         build_requires = metadata.get_build_requires()
#         if build_requires:
#             strs = str(' ').join(build_requires)
#             print strs
#     
#     if print_requires:
#         requires = metadata.get_requires()
#         if requires:
#             strs = str(' ').join(requires)
#             print strs
#     
#     if print_help:
#         if metadata.help:
#             print str(metadata.help)
#         else:
#             if not quiet:
#                 error("No 'help' entry specified in " + filepath + ".")
#             sys.exit(1)
#     
#     if print_tools:
#         tools = metadata.metadict.get("tools")
#         if tools:
#             print str(' ').join(tools)
#     
#     if (variant_num != None):
#         variants = metadata.get_variants()
#         if variants:
#             if (variant_num >= len(variants)):
#                 if not quiet:
#                     error("Variant #" + str(variant_num) + " does not exist in package.")
#                 sys.exit(1)
#             else:
#                 strs = str(' ').join(variants[variant_num])
#                 print strs
#         else:
#             if not quiet:
#                 error("Variant #" + str(variant_num) + " does not exist in package.")
#             sys.exit(1)

def setup_parser(parser):
    import rez.public_enums as enums
    parser.add_argument("-m", "--mode", dest="mode",
                        default=enums.RESOLVE_MODE_LATEST,
                        choices=[enums.RESOLVE_MODE_LATEST,
                                 enums.RESOLVE_MODE_EARLIEST,
                                 enums.RESOLVE_MODE_NONE],
                        help="set resolution mode")
    parser.add_argument("-v", "--variant", dest="variant_nums", type=int,
                        action='append',
                        help="individual variant to build")
    parser.add_argument("-t", "--time", dest="time", type=int,
                        default=0,
                        help="ignore packages newer than the given epoch time [default = current time]")
    parser.add_argument("-i", "--install-path", dest="print_install_path",
                        action="store_true", default=False,
                        help="print the path that the project would be installed to, and exit")
    parser.add_argument("-g", "--ignore-archiving", dest="ignore_archiving",
                        action="store_true", default=False,
                        help="silently ignore packages that have been archived")
    parser.add_argument("-u", "--ignore-blacklist", dest="ignore_blacklist",
                        action="store_true", default=False,
                        help="include packages that are blacklisted")
    parser.add_argument("-d", "--no-assume-dt", dest="no_assume_dt",
                        action="store_true", default=False,
                        help="do not assume dependency transitivity")
    parser.add_argument("-c", "--changelog", dest="changelog",
                        type=str,
                        help="VCS changelog")
    parser.add_argument("-r", "--release", dest="release_install",
                        action="store_true", default=False,
                        help="install packages to release directory")
    parser.add_argument("-s", "--vcs-metadata", dest="vcs_metadata",
                        type=str,
                        help="VCS metadata")

    # cmake options
    parser.add_argument("--target", dest="build_target",
                        choices=['Debug', 'Release'],
                        default="Release",
                        help="build type")
    parser.add_argument("-b", "--build-system", dest="build_system",
                        choices=sorted(BUILD_SYSTEMS.keys()),
                        type=lambda x: BUILD_SYSTEMS[x],
                        default='eclipse')
    parser.add_argument("--retain-cache", dest="retain_cache",
                        action="store_true", default=False,
                        help="retain cmake cache")

    # make options
    parser.add_argument("-n", "--no-clean", dest="no_clean",
                        action="store_true", default=False,
                        help="do not run clean prior to building")

    parser.add_argument('extra_args', nargs=argparse.REMAINDER,
                        help="remaining arguments are passed to make and cmake")

def command(opts):
    import rez.rez_filesys
    from rez.rez_util import get_epoch_time
    from . import config as rez_cli_config

    now_epoch = get_epoch_time()
    cmake_args = get_cmake_args(opts.build_system, opts.build_target,
                                opts.release_install)

    # separate out remaining args into cmake and make groups
    # e.g rez-build [args] -- [cmake args] -- [make args]
    if opts.extra_args:
        assert opts.extra_args[0] == '--'

    make_args = []
    do_build = False
    if opts.extra_args:
        arg_list = cmake_args
        for arg in opts.extra_args[1:]:
            if arg == '--':
                # switch list
                arg_list = make_args
                do_build = True
                continue
            arg_list.append(arg)

    # -n option is disallowed if not building
    if not do_build and opts.no_clean:
        error("-n option is only supported when performing a build, eg 'rez-build -n -- --'")
        sys.exit(1)

    # any packages newer than this time will be ignored. This serves two purposes:
    # 1) It stops inconsistent builds due to new packages getting released during a build;
    # 2) It gives us the ability to reproduce a build that happened in the past, ie we can make
    # it resolve the way that it did, rather than the way it might today
    if not opts.time:
        opts.time = now_epoch

    #-#################################################################################################
    # Extract info from package.yaml
    #-#################################################################################################

    if not os.path.isfile("package.yaml"):
        error("rez-build failed - no package.yaml in current directory.")
        sys.exit(1)

    metadata = _get_package_metadata(os.path.abspath("package.yaml"))
    reqs = metadata.get_requires(include_build_reqs=True) or []

    variants = _get_variants(metadata, opts.variant_nums)

    #-#################################################################################################
    # Iterate over variants
    #-#################################################################################################

    import rez.rez_release
    vcs = rez.rez_release.get_release_mode('.')
    if vcs.name == 'base':
        # we only care about version control, so ignore the base release mode
        vcs = None

    if vcs and not opts.vcs_metadata:
        url = vcs.get_url()
        opts.vcs_metadata = url if url else "(NONE)"

    build_data = metadata.metadict.get('external_build')
    if build_data:
        try:
            retrievers = SourceRetriever.get_source_retrievers(metadata)
            if retrievers:
                success = False
                for retriever in retrievers:
                    try:
                        srcdir = retriever.get_source()
                        success = True
                        break
                    except Exception as e:
                        err_msg = ''.join(traceback.format_exception_only(type(e), e))
                        error("Error retrieving source from %s: %s"
                              % (retriever.url, err_msg.rstrip()))
                if not success:
                    error("All retrievers failed to retrieve source")
                    sys.exit(1)

                if 'commands' in build_data:
                    # cleanup prevous runs
                    if os.path.exists('CMakeLists.txt'):
                        os.remove('CMakeLists.txt')
                    install_commands = build_data['commands']
                    assert isinstance(install_commands, list)
                    working_dir = build_data.get('working_dir', 'source')
                    _write_cmakelist(install_commands, srcdir, working_dir)

        except SourceRetrieverError as e:
            error(str(e))
            sys.exit(1)

    build_dir_base = os.path.abspath("build")
    build_dir_id = os.path.join(build_dir_base, ".rez-build")

    for variant_num, variant in variants:
        # set variant and create build directories
        variant_str = ' '.join(variant)
        if variant_num == -1:
            build_dir = build_dir_base
            cmake_dir_arg = "../"

            if opts.print_install_path:
                output(os.path.join(os.environ['REZ_RELEASE_PACKAGES_PATH'],
                                    metadata.name, metadata.version))
                continue
        else:
            build_dir = os.path.join(build_dir_base, str(variant_num))
            cmake_dir_arg = "../../"

            build_dir_symlink = os.path.join(build_dir_base, '_'.join(variant))
            variant_subdir = os.path.join(*variant)

            if opts.print_install_path:
                output(os.path.join(os.environ['REZ_RELEASE_PACKAGES_PATH'],
                                    metadata.name, metadata.version, variant_subdir))
                continue

            print
            print "---------------------------------------------------------"
            print "rez-build: building for variant '%s'" % variant_str
            print "---------------------------------------------------------"

        if not os.path.exists(build_dir):
            os.makedirs(build_dir)
        if variant and not os.path.islink(build_dir_symlink):
            os.symlink(os.path.basename(build_dir), build_dir_symlink)

        src_file = os.path.join(build_dir, 'build-env.sh')
        env_bake_file = os.path.join(build_dir, 'build-env.context')
        actual_bake = os.path.join(build_dir, 'build-env.actual')
        dot_file = os.path.join(build_dir, 'build-env.context.dot')
        changelog_file = os.path.join(build_dir, 'changelog.txt')

        # allow the svn pre-commit hook to identify the build directory as such
        with open(build_dir_id, 'w') as f:
            f.write('')

        # FIXME: use yaml for info.txt?
        meta_file = os.path.join(build_dir, 'info.txt')
        # store build metadata
        with open(meta_file, 'w') as f:
            import getpass
            f.write("ACTUAL_BUILD_TIME: %d"  % now_epoch)
            f.write("BUILD_TIME: %d" % opts.time)
            f.write("USER: %s" % getpass.getuser())
            # FIXME: change entry SVN to VCS
            f.write("SVN: %s" % opts.vcs_metadata)

        # store the changelog into a metafile (rez-release will specify one
        # via the -c flag)
        if not opts.changelog:
            if vcs is None:
                log = 'not under version control'
            else:
                log = vcs.get_changelog()
                assert log is not None, "RezReleaseMode '%s' has not properly implemented get_changelog()" % vcs.name
            with open(changelog_file, 'w') as f:
                f.write(log)
        else:
            shutil.copy(opts.changelog, changelog_file)

        # attempt to resolve env for this variant
        print
        print "rez-build: invoking rez-config with args:"
        #print "$opts.no_archive $opts.ignore_blacklist $opts.no_assume_dt --time=$opts.time"
        print "requested packages: %s" % (', '.join(reqs + variant))
        print "package search paths: %s" % (os.environ['REZ_PACKAGES_PATH'])

#         # Note: we pull latest version of cmake into the env
#         rez-config
#             $opts.no_archive
#             $opts.ignore_blacklist
#             --print-env
#             --time=$opts.time
#             $opts.no_assume_dt
#             --dot-file=$dot_file
#             $reqs $variant cmake=l > $env_bake_file
# 
#         if [ $? != 0 ]:
#             rm -f $env_bake_file
#             print "rez-build failed - an environment failed to resolve." >&2
#             sys.exit(1)

        # setup args for rez-config
        # TODO: provide a util which reads defaults for the cli function
        kwargs = dict(pkg=(reqs + variant + ['cmake=l']),
                      verbosity=0,
                      version=False,
                      print_env=False,
                      print_dot=False,
                      meta_info='tools',
                      meta_info_shallow='tools',
                      env_file=env_bake_file,
                      dot_file=dot_file,
                      max_fails=-1,
                      wrapper=False,
                      no_catch=False,
                      no_path_append=False,
                      print_pkgs=False,
                      quiet=False,
                      no_local=False,
                      buildreqs=False,
                      no_cache=False,
                      no_os=False)
        # copy settings that are the same between rez-build and rez-config
        kwargs.update(vars(opts))
    
        config_opts = argparse.Namespace(**kwargs)

        try:
            rez_cli_config.command(config_opts)

            # TODO: call rez_config.Resolver directly
#             resolver = dc.Resolver(opts.mode,
#                                    time_epoch=opts.time,
#                                    assume_dt=not opts.no_assume_dt,
#                                    caching=not opts.no_cache)
#             result = resolver.guarded_resolve((reqs + variant + ['cmake=l']),
#                                               dot_file)
        except Exception, err:
            error("rez-build failed - an environment failed to resolve.\n" + str(err))
            if os.path.exists(dot_file):
                os.remove(dot_file)
            if os.path.exists(env_bake_file):
                os.remove(env_bake_file)
            sys.exit(1)

        # TODO: this shouldn't be a separate step
        # create dot-file
        # rez-config --print-dot --time=$opts.time $reqs $variant > $dot_file

        text = textwrap.dedent("""\
            #!/bin/bash

            # because of how cmake works, you must cd into same dir as script to run it
            if [ "./build-env.sh" != "$0" ] ; then
                echo "you must cd into the same directory as this script to use it." >&2
                exit 1
            fi

            source %(env_bake_file)s
            export REZ_CONTEXT_FILE=%(env_bake_file)s
            env > %(actual_bake)s

            # need to expose rez-config's cmake modules in build env
            [[ CMAKE_MODULE_PATH ]] && export CMAKE_MODULE_PATH=%(rez_path)s/cmake';'$CMAKE_MODULE_PATH || export CMAKE_MODULE_PATH=%(rez_path)s/cmake

            # make sure we can still use rez-config in the build env!
            export PATH=$PATH:%(rez_path)s/bin

            echo
            echo rez-build: in new env:
            rez-context-info

            # set env-vars that CMakeLists.txt files can reference, in this way
            # we can drive the build from the package.yaml file
            export REZ_BUILD_ENV=1
            export REZ_BUILD_PROJECT_VERSION=%(version)s
            export REZ_BUILD_PROJECT_NAME=%(name)s
            """ % dict(env_bake_file=env_bake_file,
                       actual_bake=actual_bake,
                       rez_path=rez.rez_filesys._g_rez_path,
                       version=metadata.version,
                       name=metadata.name))

        if reqs:
            text += "export REZ_BUILD_REQUIRES_UNVERSIONED='%s'\n" % (' '.join([unversioned(x) for x in reqs]))

        if variant_num != -1:
            text += "export REZ_BUILD_VARIANT='%s'\n" % variant_str
            text += "export REZ_BUILD_VARIANT_UNVERSIONED='%s'\n" % (' '.join([unversioned(x) for x in variant]))
            text += "export REZ_BUILD_VARIANT_SUBDIR=/%s/\n" % variant_subdir

        if not opts.retain_cache:
            text += _format_bash_command(["rm", "-f", "CMakeCache.txt"])

        # cmake invocation
        text += _format_bash_command(["cmake", "-d", cmake_dir_arg] + cmake_args)

        if do_build:
            # TODO: determine build tool from --build-system? For now just assume make

            if not opts.no_clean:
                text += _format_bash_command(["make", "clean"])

            text += _format_bash_command(["make"] + make_args)

            with open(src_file, 'w') as f:
                f.write(text + '\n')
            _chmod(src_file, 0777)

            # run the build
            # TODO: add the 'cd' into the script itself
            p = subprocess.Popen([os.path.join('.', os.path.basename(src_file))],
                                 cwd=os.path.dirname(src_file))
            p.communicate()
            if p.returncode != 0 :
                error("rez-build failed - there was a problem building. returned code %s" % (p.returncode,))
                sys.exit(1)

        else:
            text += 'export REZ_ENV_PROMPT=">$REZ_ENV_PROMPT"\n'
            text += "export REZ_ENV_PROMPT='BUILD>'\n"
            text += "/bin/bash --rcfile %s/bin/rez-env-bashrc\n" % rez.rez_filesys._g_rez_path

            with open(src_file, 'w') as f:
                f.write(text + '\n')
            _chmod(src_file, 0777)

            if variant_num == -1:
                print "Generated %s, invoke to run cmake for this project." % src_file
            else:
                print "Generated %s, invoke to run cmake for this project's variant:(%s)" % (src_file, variant_str)


#    Copyright 2012 BlackGinger Pty Ltd (Cape Town, South Africa)
#
#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either metadata.version 3 of the License, or
#    (at your option) any later metadata.version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.
