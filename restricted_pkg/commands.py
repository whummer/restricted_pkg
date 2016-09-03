# -*- coding: utf-8 -*-
# Copyright (c) 2012-2013 Raphaël Barrois.
# Distributed under the MIT License.

import os
import re
import sys

from distutils.errors import DistutilsOptionError, DistutilsSetupError
from distutils import log
import setuptools
from setuptools.command.install import install as base_install
from setuptools.command.easy_install import easy_install as base_easy_install
from setuptools.command.upload_docs import upload_docs as base_upload_docs


from .compat import urlparse


try:
    from distutils.command.upload import upload as base_upload
except ImportError:
    from setuptools.command.upload import upload as base_upload
try:
    from distutils.command.register import register as base_register
except ImportError:
    from setuptools.command.register import register as base_register

from . import base


DEFAULT_PYPI_RC = '~/.pypirc'


def get_repo_url(pypirc, repository):
    """Fetch the RepositoryURL for a given repository, reading info from pypirc.

    Will try to find the repository in the .pypirc, including username/password.

    Args:
        pypirc (str): path to the .pypirc config file
        repository (str): URL or alias for the repository

    Returns:
        base.RepositoryURL for the repository
    """
    pypirc = os.path.abspath(os.path.expanduser(pypirc))
    pypi_config = base.PyPIConfig(pypirc)
    repo_config = pypi_config.get_repo_config(repository)
    if repo_config:
        return repo_config.get_clean_url()
    else:
        return base.RepositoryURL(repository)


class install(base_install):
    """Overridden install which adds --disable-pypi and --pypirc options."""
    user_options = base_install.user_options + [
        ('disable-pypi', None, "Don't use PyPI package index"),
        ('pypirc=', None, "Path to .pypirc configuration file"),
    ]
    boolean_options = base_install.boolean_options + ['disable-pypi']

    def initialize_options(self):
        base_install.initialize_options(self)
        self.disable_pypi = None
        self.pypirc = None


class easy_install(base_easy_install):
    """Overridden easy_install which adds a url from private_repository.

    Also handles username/password prompting for that private_repository.
    """

    user_options = base_easy_install.user_options + [
        ('disable-pypi', None, "Don't use PyPI package index"),
        ('pypirc=', None, "Path to .pypirc configuration file"),
    ]
    boolean_options = base_easy_install.boolean_options + ['disable-pypi']

    def initialize_options(self):
        base_easy_install.initialize_options(self)
        self.disable_pypi = None
        self.pypirc = None

    def finalize_options(self):
        if self.distribution.private_repository is None:
            raise DistutilsSetupError(
                "The 'private_repository' argument to the setup() call is required."
            )
        self.pypirc = self.pypirc or DEFAULT_PYPI_RC

        repo_url = get_repo_url(self.pypirc, self.distribution.private_repository)

        # Retrieve disable_pypi from install
        self.set_undefined_options('install', ('disable_pypi', 'disable_pypi'))

        if self.disable_pypi:
            log.info("Replacing PyPI with private repository %s.",
                repo_url.base_url)
            # Replace PyPI
            self.index_url = repo_url.full_url
            # Disable find_links option inherited from packages
            self.no_find_links = True

        else:
            # Clean up self.find_links
            self.find_links = self.find_links or []
            self.ensure_string_list('find_links')

            # Add custom URL
            log.info("Adding private repository %s to searched repositories.",
                repo_url.base_url)
            self.find_links.append(repo_url.full_url)

        # Parent options
        base_easy_install.finalize_options(self)


class register(base_register):
    """Overridden register command restricting upload to the private repo."""

    user_options = base_register.user_options + [
        ('pypirc=', None, "Path to .pypirc configuration file"),
    ]

    def initialize_options(self):
        base_register.initialize_options(self)
        self.pypirc = None

    def finalize_options(self):
        if self.distribution.private_repository is None:
            raise DistutilsSetupError(
                "The 'private_repository' argument to the setup() call is required."
            )

        self.pypirc = self.pypirc or DEFAULT_PYPI_RC
        package_repo = base.RepositoryURL(self.distribution.private_repository)
        repo_url = get_repo_url(self.pypirc, self.repository or package_repo.base_url)

        if self.repository and repo_url not in package_repo:
            raise DistutilsOptionError(
                "The --repository option of private packages must match the "
                "configured private repository, %s." % package_repo.base_url
            )

        log.info("Switching to private repository at %s", package_repo.base_url)
        self.repository = repo_url.base_url
        self.username = repo_url.username
        self.password = repo_url.password

        base_register.finalize_options(self)


class upload(base_upload):
    """Overridden upload command restricting upload to the private repo."""

    user_options = base_upload.user_options + [
        ('pypirc=', None, "Path to .pypirc configuration file"),
    ]

    def initialize_options(self):
        base_upload.initialize_options(self)
        self.pypirc = None

    def finalize_options(self):
        if self.distribution.private_repository is None:
            raise DistutilsSetupError(
                "The 'private_repository' argument to the setup() call is required."
            )

        self.pypirc = self.pypirc or DEFAULT_PYPI_RC
        package_repo = base.RepositoryURL(self.distribution.private_repository)
        repo_url = get_repo_url(self.pypirc, self.repository or package_repo.base_url)

        if self.repository and repo_url not in package_repo:
            raise DistutilsOptionError(
                "The --repository option of private packages must match the "
                "configured private repository, %s." % package_repo.base_url
            )

        log.info("Switching to private repository at %s", package_repo.base_url)
        self.repository = repo_url.base_url
        self.username = repo_url.username
        self.password = repo_url.password

        base_upload.finalize_options(self)

    # Since we want to be able to add custom headers, we need to copy the entire upload_file(..)
    # method from distutils.command.upload. This method prepares and creates the urllib2.Request
    # object which we will update to include the custom headers (typically for authentication
    # purposes) specified in the package's setup.py ("setup(...)") metadata.
    def upload_file_extended(self, command, pyversion, filename):
        # copy imports from distutils.command.upload
        import socket
        import platform
        from urllib2 import urlopen, Request, HTTPError
        from base64 import standard_b64encode
        import urlparse
        import cStringIO as StringIO
        from hashlib import md5
        from distutils.errors import DistutilsError
        from distutils.spawn import spawn
        from distutils import log

        encode_content_base64 = True

        # Makes sure the repository URL is compliant
        schema, netloc, url, params, query, fragments = \
            urlparse.urlparse(self.repository)
        if params or query or fragments:
            raise AssertionError("Incompatible url %s" % self.repository)

        if schema not in ('http', 'https'):
            raise AssertionError("unsupported schema " + schema)

        # Sign if requested
        if self.sign:
            gpg_args = ["gpg", "--detach-sign", "-a", filename]
            if self.identity:
                gpg_args[2:2] = ["--local-user", self.identity]
            spawn(gpg_args,
                  dry_run=self.dry_run)

        # Fill in the data - send all the meta-data in case we need to
        # register a new release
        f = open(filename,'rb')
        try:
            content = f.read()
        finally:
            f.close()
        if encode_content_base64:
            content = standard_b64encode(content)
        meta = self.distribution.metadata
        data = {
            # action
            ':action': 'file_upload',
            'protcol_version': '1',

            # identify release
            'name': meta.get_name(),
            'version': meta.get_version(),

            # file content
            'content': (os.path.basename(filename),content),
            'filetype': command,
            'pyversion': pyversion,
            'md5_digest': md5(content).hexdigest(),

            # additional meta-data
            'metadata_version' : '1.0',
            'summary': meta.get_description(),
            'home_page': meta.get_url(),
            'author': meta.get_contact(),
            'author_email': meta.get_contact_email(),
            'license': meta.get_licence(),
            'description': meta.get_long_description(),
            'keywords': meta.get_keywords(),
            'platform': meta.get_platforms(),
            'classifiers': meta.get_classifiers(),
            'download_url': meta.get_download_url(),
            # PEP 314
            'provides': meta.get_provides(),
            'requires': meta.get_requires(),
            'obsoletes': meta.get_obsoletes(),
            }
        comment = ''
        if command == 'bdist_rpm':
            dist, version, id = platform.dist()
            if dist:
                comment = 'built for %s %s' % (dist, version)
        elif command == 'bdist_dumb':
            comment = 'built for %s' % platform.platform(terse=1)
        data['comment'] = comment

        if self.sign:
            data['gpg_signature'] = (os.path.basename(filename) + ".asc",
                                     open(filename+".asc").read())

        # set up the authentication
        auth = "Basic " + standard_b64encode(self.username + ":" +
                                             self.password)

        # Build up the MIME payload for the POST data
        boundary = '--------------GHSKFJDLGDS7543FJKLFHRE75642756743254'
        sep_boundary = '\r\n--' + boundary
        end_boundary = sep_boundary + '--\r\n'
        body = StringIO.StringIO()
        for key, value in data.items():
            # handle multiple entries for the same name
            if not isinstance(value, list):
                value = [value]
            for value in value:
                if isinstance(value, tuple):
                    fn = ';filename="%s"' % value[0]
                    value = value[1]
                else:
                    fn = ""

                body.write(sep_boundary)
                body.write('\r\nContent-Disposition: form-data; name="%s"' % key)
                body.write(fn)
                if encode_content_base64 and fn:
                    body.write('\r\nContent-Transfer-Encoding: base64')
                body.write("\r\n\r\n")
                body.write(value)
                if value and value[-1] == '\r':
                    body.write('\n')  # write an extra newline (lurve Macs)
        body.write(end_boundary)
        body = body.getvalue()

        self.announce("Submitting %s to %s" % (filename, self.repository), log.INFO)

        # build the Request
        headers = {'Content-type':
                        'multipart/form-data; boundary=%s' % boundary,
                   'Content-length': str(len(body)),
                   'Authorization': auth}
        # add custom headers, if any
        if self.distribution.custom_headers:
            headers.update(self.distribution.custom_headers)

        request = Request(self.repository, data=body,
                          headers=headers)
        # send the data
        try:
            result = urlopen(request)
            status = result.getcode()
            reason = result.msg
            if self.show_response:
                msg = '\n'.join(('-' * 75, result.read(), '-' * 75))
                self.announce(msg, log.INFO)
        except socket.error, e:
            self.announce(str(e), log.ERROR)
            raise
        except HTTPError, e:
            status = e.code
            reason = e.msg

        if status == 200:
            self.announce('Server response (%s): %s' % (status, reason),
                          log.INFO)
        else:
            msg = 'Upload failed (%s): %s' % (status, reason)
            self.announce(msg, log.ERROR)
            raise DistutilsError(msg)

    def upload_file(self, command, pyversion, filename):
        if not self.distribution.custom_headers:
            # use base version
            return base_upload.upload_file(command, pyversion, filename)
        else:
            # use extended version that supports custom headers
            return self.upload_file_extended(command, pyversion, filename)


class upload_docs(base_upload_docs):
    """Overridden upload_docs command restricting upload to the private repo."""

    user_options = base_upload_docs.user_options + [
        ('pypirc=', None, "Path to .pypirc configuration file"),
    ]

    def initialize_options(self):
        base_upload_docs.initialize_options(self)
        self.pypirc = None

    def finalize_options(self):
        if self.distribution.private_repository is None:
            raise DistutilsSetupError(
                "The 'private_repository' argument to the setup() call is required."
            )

        self.pypirc = self.pypirc or DEFAULT_PYPI_RC
        package_repo = base.RepositoryURL(self.distribution.private_repository)
        repo_url = get_repo_url(self.pypirc, self.repository or package_repo.base_url)

        if self.repository and repo_url not in package_repo:
            raise DistutilsOptionError(
                "The --repository option of private packages must match the "
                "configured private repository, %s." % package_repo.base_url
            )

        log.info("Switching to private repository at %s", package_repo.base_url)
        self.repository = repo_url.base_url
        self.username = repo_url.username
        self.password = repo_url.password

        base_upload_docs.finalize_options(self)


def setup(**kwargs):
    """Custom setup() function, inserting our custom classes."""

    cmdclass = kwargs.setdefault('cmdclass', {})
    cmdclass['easy_install'] = easy_install
    cmdclass['install'] = install
    cmdclass['register'] = register
    cmdclass['upload'] = upload
    cmdclass['upload_docs'] = upload_docs
    return setuptools.setup(**kwargs)
