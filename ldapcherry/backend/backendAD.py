# -*- coding: utf-8 -*-
# vim:set expandtab tabstop=4 shiftwidth=4:
#
# License MIT
# LdapCherry
# Copyright (c) 2014 Carpentier Pierre-Francois

import ldapcherry.backend.backendLdap
import cherrypy
import ldap
import ldap.modlist as modlist
import ldap.filter
import logging
import ldapcherry.backend
from ldapcherry.exceptions import UserDoesntExist, GroupDoesntExist
import os
import re
import sys


class CaFileDontExist(Exception):
    def __init__(self, cafile):
        self.cafile = cafile
        self.log = "CA file %(cafile)s don't exist" % {'cafile': cafile}


class MissingAttr(Exception):
    def __init__(self):
        self.log = 'attributes "cn" and "unicodePwd" must be declared ' \
           'in attributes.yml for all Active Directory backends.'


NO_ATTR = 0
DISPLAYED_ATTRS = 1
LISTED_ATTRS = 2
ALL_ATTRS = 3

# UserAccountControl Attribute/Flag Values
# For details, look at:
# https://support.microsoft.com/en-us/kb/305144
SCRIPT = 0x0001
ACCOUNTDISABLE = 0x0002
HOMEDIR_REQUIRED = 0x0008
LOCKOUT = 0x0010
PASSWD_NOTREQD = 0x0020
PASSWD_CANT_CHANGE = 0x0040
ENCRYPTED_TEXT_PWD_ALLOWED = 0x0080
TEMP_DUPLICATE_ACCOUNT = 0x0100
NORMAL_ACCOUNT = 0x0200
INTERDOMAIN_TRUST_ACCOUNT = 0x0800
WORKSTATION_TRUST_ACCOUNT = 0x1000
SERVER_TRUST_ACCOUNT = 0x2000
DONT_EXPIRE_PASSWORD = 0x10000
MNS_LOGON_ACCOUNT = 0x20000
SMARTCARD_REQUIRED = 0x40000
TRUSTED_FOR_DELEGATION = 0x80000
NOT_DELEGATED = 0x100000
USE_DES_KEY_ONLY = 0x200000
DONT_REQ_PREAUTH = 0x400000
PASSWORD_EXPIRED = 0x800000
TRUSTED_TO_AUTH_FOR_DELEGATION = 0x1000000
PARTIAL_SECRETS_ACCOUNT = 0x04000000
# Generated by the followin command:

# samba-tool group list | \
# while read line; \
# do
# ldapsearch -x -h localhost -D "administrator@dc.ldapcherry.org" \
#     -w qwertyP455 -b "dc=dc,dc=ldapcherry,dc=org"  "(cn=$line)" dn; \
# done | grep -e "dn: .*CN=Builtin" | \
# sed "s/dn: CN=\(.*\),CN=.*/'\1',/"

AD_BUILTIN_GROUPS = [
    'Pre-Windows 2000 Compatible Access',
    'Windows Authorization Access Group',
    'Certificate Service DCOM Access',
    'Network Configuration Operators',
    'Terminal Server License Servers',
    'Incoming Forest Trust Builders',
    'Performance Monitor Users',
    'Cryptographic Operators',
    'Distributed COM Users',
    'Performance Log Users',
    'Remote Desktop Users',
    'Account Operators',
    'Event Log Readers',
    'Backup Operators',
    'Server Operators',
    'Print Operators',
    'Administrators',
    'Replicator',
    'IIS_IUSRS',
    'Guests',
    'Users',
]


class Backend(ldapcherry.backend.backendLdap.Backend):

    def __init__(self, config, logger, name, attrslist, key):
        self.config = config
        self._logger = logger
        self.backend_name = name
        self.backend_display_name = self.get_param('display_name')
        self.domain = self.get_param('domain')
        self.login = os.getenv("AD_LOGIN", self.get_param('login'))
        basedn = 'dc=' + re.sub(r'\.', ',DC=', self.domain)
        self.binddn = self.login + '@' + self.domain
        self.bindpassword = os.getenv("PASSWORD", self.get_param('password'))
        self.ca = self.get_param('ca', False)
        self.checkcert = self.get_param('checkcert', 'on')
        self.starttls = self.get_param('starttls', 'off')
        self.uri = self.get_param('uri')
        self.timeout = self.get_param('timeout', 1)
        self.userdn = 'OU=Users,OU=' + self.domain.split(".")[0] + "," + basedn
        self.groupdn = self.userdn
        self.builtin = 'CN=Builtin,' + basedn
        self.user_filter_tmpl = '(sAMAccountName=%(username)s)'
        self.group_filter_tmpl = '(member=%(userdn)s)'
        self.search_filter_tmpl = '(&(|(sAMAccountName=%(searchstring)s)' \
            '(cn=%(searchstring)s*)' \
            '(name=%(searchstring)s*)' \
            '(sn=%(searchstring)s*)' \
            '(givenName=%(searchstring)s*)' \
            '(cn=%(searchstring)s*))' \
            '(&(objectClass=person)' \
            '(objectClass=user)' \
            '(!(objectClass=computer)))' \
            ')'
        self.dn_user_attr = 'cn'
        self.key = 'sAMAccountName'
        self.objectclasses = [
            self._byte_p23('top'),
            self._byte_p23('person'),
            self._byte_p23('organizationalPerson'),
            self._byte_p23('user'),
            self._byte_p23('posixAccount'),
            ]
        self.group_attrs = {
            'member': "%(dn)s"
            }

        self.attrlist = []
        self.group_attrs_keys = []
        for a in attrslist:
            self.attrlist.append(self._byte_p2(a))

        if self._byte_p2('cn') not in self.attrlist:
            raise MissingAttr()

        if self._byte_p2('unicodePwd') not in self.attrlist:
            raise MissingAttr()

    if sys.version < '3':
        @staticmethod
        def _tobyte(in_int):
            return str(in_int)
    else:
        @staticmethod
        def _tobyte(in_int):
            return in_int.to_bytes(4, byteorder='big')

    def _search_group(self, searchfilter, groupdn):
        searchfilter = self._byte_p2(searchfilter)
        ldap_client = self._bind()
        try:
            r = ldap_client.search_s(
                groupdn,
                ldap.SCOPE_SUBTREE,
                searchfilter,
                attrlist=['CN']
                )
        except Exception as e:
            ldap_client.unbind_s()
            self._exception_handler(e)

        ldap_client.unbind_s()
        return r

    def _build_groupdn(self, groups):
        ad_groups = []
        for group in groups:
            if group in AD_BUILTIN_GROUPS:
                ad_groups.append('cn=' + group + ',' + self.builtin)
            else:
                ad_groups.append('cn=' + group + ',' + self.groupdn)
        return ad_groups

    def _set_password(self, name, password, by_cn=True):
        unicode_pass = '\"' + password + '\"'
        password_value = unicode_pass.encode('utf-16-le')

        ldap_client = self._bind()

        if by_cn:
            dn = self._byte_p2('CN=%(cn)s,%(user_dn)s' % {
                        'cn': name,
                        'user_dn': self.userdn
                       })
        else:
            dn = self._byte_p2(name)

        ldap_client.modify_s(
            dn,
            [(ldap.MOD_REPLACE, 'unicodePwd', [password_value])]
        )
        return

        attrs = {}

        attrs['unicodePwd'] = self._modlist(self._byte_p2(password_value))

        ldif = modlist.modifyModlist({'unicodePwd': 'tmp'}, attrs)
        ldap_client.modify_s(dn, ldif)

        del(attrs['unicodePwd'])
        attrs['UserAccountControl'] = self._modlist(
            self._tobyte(NORMAL_ACCOUNT)
        )
        ldif = modlist.modifyModlist({'UserAccountControl': 'tmp'}, attrs)
        ldap_client.modify_s(dn, ldif)

    def add_user(self, attrs):
        password = attrs['unicodePwd']
        del(attrs['unicodePwd'])
        attrs['userPrincipalName'] = '%(name)s@%(domain)s' % {
            'name': attrs['sAMAccountName'], 'domain': self.domain
        }
        attrs['displayName'] = attrs['cn']
        super(Backend, self).add_user(attrs)

        ldap_client = self._bind()

        dn = self._byte_p2('CN=%(cn)s,%(user_dn)s' % {
            'cn': attrs['cn'], 'user_dn': self.userdn
        })

        # Set password
        encoded_password = '"{}"'.format(password).encode('utf-16-le')
        ldap_client.modify_s(
            dn,
            [(ldap.MOD_REPLACE, 'unicodePwd', [encoded_password])]
        )

        # Enable user account
        ldap_client.modify_s(
            dn,
            [(ldap.MOD_REPLACE, 'UserAccountControl', [b'512'])]
        )

        ldap_client.unbind_s()

    def set_attrs(self, username, attrs):
        if 'unicodePwd' in attrs:
            password = attrs['unicodePwd']
            del(attrs['unicodePwd'])
            userdn = self._get_user(self._byte_p2(username), NO_ATTR)
            self._set_password(userdn, password, False)
        super(Backend, self).set_attrs(username, attrs)

    def add_to_groups(self, username, groups):
        ad_groups = self._build_groupdn(groups)
        super(Backend, self).add_to_groups(username, ad_groups)

    def del_from_groups(self, username, groups):
        ad_groups = self._build_groupdn(groups)
        super(Backend, self).del_from_groups(username, ad_groups)

    def get_groups(self, username):
        username = ldap.filter.escape_filter_chars(username)
        userdn = self._get_user(self._byte_p2(username), NO_ATTR)

        searchfilter = self.group_filter_tmpl % {
            'userdn': userdn,
            'username': username
        }

        groups = self._search_group(searchfilter, self.groupdn)
        groups = groups + self._search_group(searchfilter, self.builtin)
        ret = []
        self._logger(
            severity=logging.DEBUG,
            msg="%(backend)s: groups of '%(user)s' are %(groups)s" % {
                'user': username,
                'groups': str(groups),
                'backend': self.backend_name
                }
        )

        for entry in groups:
            ret.append(self._uni(entry[1]['cn'][0]))
        return ret

    def auth(self, username, password):

        binddn = username + '@' + self.domain
        if binddn is not None:
            ldap_client = self._connect()
            try:
                ldap_client.simple_bind_s(
                    self._byte_p2(binddn),
                    self._byte_p2(password)
                )
            except ldap.INVALID_CREDENTIALS:
                ldap_client.unbind_s()
                return False
            ldap_client.unbind_s()
            return True
        else:
            return False
