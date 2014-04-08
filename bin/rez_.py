#!!REZ_PYTHON_BINARY!
from __future__ import with_statement
import os
import sys
import inspect
import argparse
import pkgutil
import rez.cli
import textwrap

COMMON_PARSER_ARGS = [
    ('--logfile',
        {'help': 'log all stdout/stderr output to the given file, in'
         ' addition to printing to the screen'}),
    ('--logfile-overwrite',
        {'action': 'store_true', 'help':'log all stdout/stderr output to'
         ' the given file, in addition to printing to the screen'}),
]

def get_parser_defaults(parser):
    return dict((act.dest, act.default) for act in parser._actions)

def subpackages(packagemod):
    """
    Given a module object, returns an iterator which yields a tuple (modulename, moduleobject, ispkg)
    for the given module and all it's submodules/subpackages.
    """
    modpkgs = []
    modpkgs_names = set()
    if hasattr(packagemod, '__path__'):
        yield packagemod.__name__, True
        for importer, modname, ispkg in pkgutil.walk_packages(packagemod.__path__,
                                                              packagemod.__name__+'.'):
            yield modname, ispkg
    else:
        yield packagemod.__name__, False

class DescriptionHelpFormatter(argparse.HelpFormatter):
    """Help message formatter which retains double-newlines in descriptions
    and adds default values
    """

    def _fill_text(self, text, width, indent):
        #text = self._whitespace_matcher.sub(' ', text).strip()
        text = text.strip()
        return '\n\n'.join([textwrap.fill(x, width,
                                          initial_indent=indent,
                                          subsequent_indent=indent) for x in text.split('\n\n')])

    def _get_help_string(self, action):
        help = action.help
        if '%(default)' not in action.help:
            if action.default is not argparse.SUPPRESS:
                defaulting_nargs = [argparse.OPTIONAL, argparse.ZERO_OR_MORE]
                if action.option_strings or action.nargs in defaulting_nargs:
                    help += ' (default: %(default)s)'
        return help

class SetupRezSubParser(object):
    """
    Callback class for lazily setting up rez sub-parsers.
    """
    def __init__(self, module_name):
        self.module_name = module_name

    def __call__(self, parser_name, parser):
        mod = self.get_module()

        if not mod.__doc__:
            rez.cli.error("command module %s must have a module-level "
                          "docstring (used as the command help)" % self.module_name)
        if not hasattr(mod, 'command'):
            rez.cli.error("command module %s must provide a command() "
                          "function" % self.module_name)
        if not hasattr(mod, 'setup_parser'):
            rez.cli.error("command module %s  must provide a setup_parser() "
                          "function" % self.module_name)

        mod.setup_parser(parser)
        parser.description = mod.__doc__
        parser.set_defaults(func=mod.command)
        # add the common args to the subparser
        for arg, arg_settings in COMMON_PARSER_ARGS:
            parser.add_argument(arg, **arg_settings)

        # optionally, return the brief help line for this sub-parser
        brief = mod.__doc__.strip('\n').split('\n')[0]
        return brief

    def get_module(self):
        if self.module_name not in sys.modules:
            try:
                #mod = importer.find_module(modname).load_module(modname)
                __import__(self.module_name, globals(), locals(), [], -1)
            except Exception, e:
                rez.cli.error("importing %s: %s" % (self.module_name, e))
                return None
        return sys.modules[self.module_name]

class LazySubParsersAction(argparse._SubParsersAction):
    """
    argparse Action which calls the `setup_subparser` action provided to
    `LazyArgumentParser`.
    """
    def __call__(self, parser, namespace, values, option_string=None):
        parser_name = values[0]

        # select the parser
        try:
            parser = self._name_parser_map[parser_name]
        except KeyError:
            tup = parser_name, ', '.join(self._name_parser_map)
            msg = _('unknown parser %r (choices: %s)' % tup)
            raise ArgumentError(self, msg)

        self._setup_subparser(parser_name, parser)

        caller = super(LazySubParsersAction, self).__call__
        return caller(parser, namespace, values, option_string)

    def _setup_subparser(self, parser_name, parser):
        if hasattr(parser, 'setup_subparser'):
            help = parser.setup_subparser(parser_name, parser)
            if help is not None:
                help_action = self._find_choice_action(parser_name)
                if help_action is not None:
                    help_action.help = help

    def _find_choice_action(self, parser_name):
        for help_action in self._choices_actions:
            if help_action.dest == parser_name:
                return help_action

class LazyArgumentParser(argparse.ArgumentParser):
    """
    ArgumentParser sub-class which accepts an additional `setup_subparser`
    argument for lazy setup of sub-parsers.

    `setup_subparser` should be passed a function with the signature:
        setup_subparser(parser_name, subparser)
    
    The function will be called to setup the sub-parser before it parses any
    input, thus it can be used to add additional arguments and fill out the
    sub-parser's 'description' attribute, which is used when help is requested for
    the sub-parser.

    The function can optionally return a help string, which will be used as the brief
    description when help is requested for the sub-parser's parent parser.
    """
    def __init__(self, *args, **kwargs):
        self.setup_subparser = kwargs.pop('setup_subparser', None)
        super(LazyArgumentParser, self).__init__(*args, **kwargs)
        self.register('action', 'parsers', LazySubParsersAction)
        # self.register('action', 'help', LazyHelpAction)

    def format_help(self):
        """
        sets up all sub-parsers when help is requested
        """
        if self._subparsers:
            for action in self._subparsers._actions:
                if isinstance(action, LazySubParsersAction):
                    for parser_name, parser in action._name_parser_map.iteritems():
                        action._setup_subparser(parser_name, parser)
        return super(LazyArgumentParser, self).format_help()

def spawn_logged_subprocess(logfile, overwrite, args):
    import subprocess
    import datetime
    
    # remove the '--logfile' arg from the given args
    new_args = list(args)
    try:
        logfile_arg_start = new_args.index('--logfile')
    except ValueError:
        # if using "--logfile=myfile.txt" syntax, find the arg, and strip
        # out only that one
        for i, arg in enumerate(new_args):
            if arg.startswith('--logfile='):
                logfile_arg_start = i
                logfile_arg_end = i + 1
                break
        else:
            raise RuntimeError("logfile arg was given, but could not find in %r"
                               % new_args)
    else:
        # if using "--logfile myfile.txt" syntax, strip out two args
        logfile_arg_end = logfile_arg_start + 2
    del new_args[logfile_arg_start:logfile_arg_end]

    # set up the logdir    
    logdir = os.path.dirname(logfile)
    if logdir and not os.path.isdir(logdir):
        try:
            os.mkdir(logdir)
        except Exception:
            print "error creating logdir"

    tee_args = ['tee']
    if overwrite:
        file_mode = 'w'
    else:
        file_mode = 'a'
        tee_args.append('-a')
        
    # write a small header, with timestamp, to the logfile -- helps separate
    # different runs if appending
    with open(logfile, file_mode) as file_handle:
        file_handle.write('='* 80 + '\n')
        file_handle.write('%s - CWD: %s\n' % (datetime.datetime.now(),
                                              os.getcwd()))
        file_handle.write('%s\n' % ' '.join(args))

    # spawn a new subprocess, without the --logfile arg, and pipe the output
    # from it through tee...
    new_environ = dict(os.environ)
    current_logs = new_environ.get('REZ_ACTIVE_LOGS', '').split(':')
    current_logs.append(logfile)
    new_environ['REZ_ACTIVE_LOGS'] = ':'.join(current_logs)
    
    rez_proc = subprocess.Popen(new_args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=new_environ)
        
    tee = subprocess.Popen(tee_args + [logfile], stdin=rez_proc.stdout)
    tee.communicate()


@rez.cli.redirect_to_stderr
def main():
    parser = LazyArgumentParser("rez")
    
    # add args common to all subcommands... we add them both to the top parser,
    # AND to the subparsers, for two reasons:
    #  1) this allows us to do EITHER "rez --logfile=foo build" OR
    #     "rez build --logfile=foo"
    #  2) this allows the flags to be used when using either "rez" or
    #     "rez-build" - ie, this will work: "rez-build --logfile=foo"

    for arg, arg_settings in COMMON_PARSER_ARGS:
        parser.add_argument(arg, **arg_settings)

    subparsers = []
    parents = []
    for name, ispkg in subpackages(rez.cli):
        cmdname = name.split('.')[-1].replace('_', '-')
        if ispkg:
            if cmdname == 'cli':
                title = 'commands'
            else:
                title = (cmdname + ' subcommands')
            subparser = parser.add_subparsers(dest='cmd',
                                              metavar='COMMAND')
            subparsers.append(subparser)
            parents.append(name)
            continue
        elif not name.startswith(parents[-1]):
            parents.pop()
            subparsers.pop()

        subparsers[-1].add_parser(cmdname,
                                  help='',  # required so that it can be setup later
                                  formatter_class=DescriptionHelpFormatter,
                                  setup_subparser=SetupRezSubParser(name)
                                              )

    args = parser.parse_args()
    
    if args.logfile:
        # check to see if we're in a subprocess, and a parent process is already
        # logging to the given logfile...
        current_logs = os.environ.get('REZ_ACTIVE_LOGS', '').split(':')
        # standardize the logfile, so we can compare it...
        logfile = os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(args.logfile))))
        if logfile not in current_logs:
            spawn_logged_subprocess(logfile, args.logfile_overwrite, sys.argv)
            return
     
    args.func(args)

if __name__ == '__main__':
    main()
