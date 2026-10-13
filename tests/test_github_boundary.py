import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from lofgren_intelligence.hosted.github_boundary import (
    GitHubGrant, GitHubAccessDenied, authorize_repository, connection_status)


class GitHubBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.grant = GitHubGrant("alice", 1, 20, frozenset({100}),
                                 frozenset({"metadata:read", "contents:read"}),
                                 self.now + timedelta(minutes=5))

    def authorize(self, **changes):
        args = dict(authenticated_user_id="alice", repository_id=100,
                    permission="contents:read", grant=self.grant, now=self.now)
        args.update(changes)
        return authorize_repository(**args)

    def test_selected_read_only_repository(self):
        self.assertEqual(self.authorize(), (20, 100))

    def test_other_user_and_unselected_repository_denied(self):
        for changes in ({"authenticated_user_id": "bob"}, {"repository_id": 101},
                        {"authenticated_user_id": ""}, {"repository_id": True}):
            with self.subTest(changes=changes), self.assertRaises(GitHubAccessDenied):
                self.authorize(**changes)

    def test_write_and_ungranted_read_denied(self):
        for permission in ("contents:write", "administration:write", "issues:read", "", [], {}):
            with self.subTest(permission=permission), self.assertRaises(GitHubAccessDenied):
                self.authorize(permission=permission)
        with self.assertRaises(GitHubAccessDenied):
            self.authorize(grant=replace(self.grant, permissions=frozenset()))

    def test_revoked_expired_naive_or_invalid_identity_denied(self):
        for grant in (replace(self.grant, revoked=True),
                      replace(self.grant, expires_at=self.now),
                      replace(self.grant, expires_at=self.now.replace(tzinfo=None)),
                      replace(self.grant, github_user_id=0),
                      replace(self.grant, installation_id=True)):
            with self.subTest(grant=grant), self.assertRaises(GitHubAccessDenied):
                self.authorize(grant=grant)

    def test_public_connection_status_does_not_promise_onboarding(self):
        status = connection_status()
        self.assertFalse(status["available"])
        self.assertFalse(status["public_mcp_requires_github"])
        self.assertFalse(status["github_identity_grants_repository_access"])
        self.assertFalse(status["github_connection_connects_ai_client"])
