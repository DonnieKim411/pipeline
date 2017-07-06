import os
from distutils.util import strtobool

from fabric.api import local, abort, run, sudo
from fabric.context_managers import cd, settings, hide, shell_env
from fabric.contrib.console import confirm
from getpass import getpass
from fabric.utils import puts

from fabric.state import env
env.builddir = env.builddir if hasattr(env, 'builddir') else '/tmp/'

def on(which):
    env.hosts = getattr(env, which).split(',')

def ssh():
    for host in env.hosts:
        local('ssh-copy-id {}'.format(host))

def with_sudo():
    """
    Prompts and sets the sudo password for all following commands.

    Use like

    fab with_sudo command
    """
    env.sudo_password = getpass('Please enter sudo password: ')
    env.password = env.sudo_password

def build_base(nocache=False, pull=False, repo='cajal'):
    nocache = strtobool(str(nocache))
    pull = strtobool(str(pull))
    with cd(env.builddir):
        with settings(warn_only=True):
            puts("Cleaning up")
            run("rm -rf pipeline")
        run('git clone https://github.com/{}/pipeline.git'.format(repo))
    with cd(env.builddir + '/pipeline'):
        args = ''
        if nocache:
            args += ' --no-cache'
        if pull:
            args += ' --pull'

        sudo('docker build {} -t ninai/pipeline:base -f Dockerfiles/Dockerfile.base .'.format(args))

def push_base():
    sudo('docker push ninai/pipeline:base')

def build_latest(nocache=False, pull=True, repo='cajal'):
    nocache = strtobool(str(nocache))
    pull = strtobool(str(pull))
    with cd(env.builddir):
        with settings(warn_only=True):
            puts("Cleaning up")
            run("rm -rf pipeline")
        run('git clone https://github.com/{}/pipeline.git'.format(repo))
    with cd(env.builddir + '/pipeline'):
        args = ''
        if nocache:
            args += ' --no-cache'
        if pull:
            args += ' --pull'

        sudo('docker build {} -t ninai/pipeline:latest .'.format(args))

def push_latest():
    sudo('docker push ninai/pipeline:latest')

