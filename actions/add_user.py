#!/usr/bin/python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from subprocess import (
    check_call,
    CalledProcessError
)

from charmhelpers.core.hookenv import (
    action_get,
    config,
    action_set,
    action_fail,
    leader_get,
    log,
)

from lib.swift_utils import (
    try_initialize_swauth,
)

from charmhelpers.contrib.hahelpers.cluster import (
    determine_api_port,
)


def add_user():
    """Add a swauth user to swift."""
    if config('auth-type') == 'swauth':
        try_initialize_swauth()
        account = action_get('account')
        username = action_get('username')
        password = action_get('password')
        bind_port = config('bind-port')
        bind_port = determine_api_port(bind_port, singlenode_mode=True)
        success = True
        try:
            check_call([
                "swauth-add-user",
                "-A", "http://localhost:{}/auth/".format(bind_port),
                "-K", leader_get('swauth-admin-key'),
                "-a", account, username, password])
        except CalledProcessError as e:
            success = False
            log("Has a problem adding user: {}".format(e.output))
            action_fail(
                "Adding user {} failed with: \"{}\""
                .format(username, e.message))
        if success:
            message = "Successfully added the user {}".format(username)
            action_set({
                'add-user.result': 'Success',
                'add-user.message': message,
            })

if __name__ == '__main__':
    add_user()
