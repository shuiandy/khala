"""Refresh token rotation: one retry within the grace period, reuse after it revokes the chain, and every exchange
checks again that the sign-in is still live."""
import asyncio
import time
import unittest

from mcp.server.auth.provider import TokenError

from base import ALICE, Base
from khala.db import digest
from khala.oauth import REFRESH_GRACE

MCP = {"Accept": "application/json, text/event-stream"}


class RefreshTests(Base):
    def refresh(self, client_id, token):
        return self.client.post("/token", data={"grant_type": "refresh_token", "refresh_token": token,
                                                "client_id": client_id})

    def works(self, access):
        r = self.client.post("/mcp", headers=dict(MCP, Authorization="Bearer " + access),
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return r.status_code == 200

    def reuse_events(self):
        return self.db.q("SELECT * FROM audit_events WHERE action='token.reuse_detected'")

    def test_a_lost_response_can_be_retried_once_and_a_third_use_revokes_the_chain(self):
        tok, cid = self.login(ALICE)
        first = self.refresh(cid, tok["refresh_token"])
        self.assertEqual(first.status_code, 200)
        retry = self.refresh(cid, tok["refresh_token"])             # the client never saw the first response
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(self.works(first.json()["access_token"]))
        self.assertTrue(self.works(retry.json()["access_token"]))
        self.assertEqual(self.reuse_events(), [])

        third = self.refresh(cid, tok["refresh_token"])
        self.assertEqual(third.status_code, 400)
        for t in (first.json(), retry.json()):
            self.assertFalse(self.works(t["access_token"]))
            self.assertEqual(self.refresh(cid, t["refresh_token"]).status_code, 400)
        events = self.reuse_events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["account_id"], events[0]["target"]), (self.alice_id, cid))
        self.web_login(ALICE)
        self.assertIn("was used again after it had been replaced", self.client.get("/app").text)
        # whoever holds the old token cannot keep filling the audit log with it
        for _ in range(3):
            self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 400)
        self.assertEqual(len(self.reuse_events()), 1)

    def test_reuse_after_the_grace_period_revokes_the_chain(self):
        tok, cid = self.login(ALICE)
        new = self.refresh(cid, tok["refresh_token"]).json()
        self.db.q("UPDATE tokens SET rotated_at=? WHERE token_hash=?", time.time() - REFRESH_GRACE - 1,
                  digest(tok["refresh_token"]))
        self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 400)
        self.assertFalse(self.works(new["access_token"]))
        self.assertEqual(self.refresh(cid, new["refresh_token"]).status_code, 400)
        self.assertEqual(len(self.reuse_events()), 1)

    def test_the_chain_revocation_leaves_other_sign_ins_alone(self):
        tok, cid = self.login(ALICE)
        other, other_cid = self.login(ALICE)                        # another client, another agent
        self.refresh(cid, tok["refresh_token"])
        self.db.q("UPDATE tokens SET rotated_at=0 WHERE token_hash=?", digest(tok["refresh_token"]))
        self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 400)
        self.assertTrue(self.works(other["access_token"]))
        self.assertEqual(self.refresh(other_cid, other["refresh_token"]).status_code, 200)

    def test_revoking_a_refresh_token_ends_its_sign_in_and_is_not_reported_as_theft(self):
        tok, cid = self.login(ALICE)
        other, _ = self.login(ALICE)
        r = self.client.post("/revoke", data={"token": tok["refresh_token"], "client_id": cid, "client_secret": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 400)
        self.assertFalse(self.works(tok["access_token"]))           # RFC 7009: the grant's access tokens end too
        self.assertTrue(self.works(other["access_token"]))
        self.assertEqual(self.reuse_events(), [])

    def test_revoking_a_just_rotated_token_closes_its_grace_period(self):
        tok, cid = self.login(ALICE)
        self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 200)
        self.client.post("/revoke", data={"token": tok["refresh_token"], "client_id": cid, "client_secret": ""})
        self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 400)

    # ---------- the provider directly: what happens between load and exchange ----------
    def provider_pair(self):
        tok, cid = self.login(ALICE)
        provider = self.app.state.provider
        client = asyncio.run(provider.get_client(cid))
        return provider, client, tok, cid

    def test_an_agent_revoked_between_load_and_exchange_gets_nothing(self):
        provider, client, tok, cid = self.provider_pair()
        loaded = asyncio.run(provider.load_refresh_token(client, tok["refresh_token"]))
        self.assertIsNotNone(loaded)
        self.db.revoke_agent(self.db.agent_for_client(self.alice_id, cid)["id"])
        with self.assertRaises(TokenError):
            asyncio.run(provider.exchange_refresh_token(client, loaded, loaded.scopes))
        self.assertEqual(self.reuse_events(), [])                  # a revocation is not reported as theft

    def test_a_token_revoked_between_load_and_exchange_is_not_reported_as_theft(self):
        provider, client, tok, cid = self.provider_pair()
        loaded = asyncio.run(provider.load_refresh_token(client, tok["refresh_token"]))
        self.client.post("/revoke", data={"token": tok["refresh_token"], "client_id": cid, "client_secret": ""})
        with self.assertRaises(TokenError):
            asyncio.run(provider.exchange_refresh_token(client, loaded, loaded.scopes))
        self.assertEqual(self.reuse_events(), [])

    def test_a_revocation_from_another_process_mid_exchange_is_not_reported_as_theft(self):
        provider, client, tok, cid = self.provider_pair()
        self.assertEqual(self.refresh(cid, tok["refresh_token"]).status_code, 200)   # rotated, still in its grace
        loaded = asyncio.run(provider.load_refresh_token(client, tok["refresh_token"]))
        self.assertIsNotNone(loaded)
        changed, db = self.db.changed, self.db

        def revoke_first(sql, *args):                              # another worker revokes just before our UPDATE
            db.changed = changed
            db.q("UPDATE tokens SET revoked=1, rotated_at=NULL WHERE token_hash=?", digest(tok["refresh_token"]))
            return changed(sql, *args)
        db.changed = revoke_first
        self.addCleanup(setattr, db, "changed", changed)
        with self.assertRaises(TokenError):
            asyncio.run(provider.exchange_refresh_token(client, loaded, loaded.scopes))
        self.assertEqual(self.reuse_events(), [])

    def test_a_token_row_that_vanished_is_not_exchanged(self):
        provider, client, tok, _ = self.provider_pair()
        loaded = asyncio.run(provider.load_refresh_token(client, tok["refresh_token"]))
        self.db.q("DELETE FROM tokens WHERE token_hash=?", digest(tok["refresh_token"]))
        with self.assertRaises(TokenError):
            asyncio.run(provider.exchange_refresh_token(client, loaded, loaded.scopes))

    def test_concurrent_exchanges_of_one_token_succeed_at_most_twice(self):
        provider, client, tok, _ = self.provider_pair()
        loads = [asyncio.run(provider.load_refresh_token(client, tok["refresh_token"])) for _ in range(3)]
        self.assertTrue(all(loads))                               # all three passed the check before any exchange
        asyncio.run(provider.exchange_refresh_token(client, loads[0], loads[0].scopes))
        asyncio.run(provider.exchange_refresh_token(client, loads[1], loads[1].scopes))
        with self.assertRaises(TokenError):
            asyncio.run(provider.exchange_refresh_token(client, loads[2], loads[2].scopes))
        self.assertEqual(len(self.reuse_events()), 1)


if __name__ == "__main__":
    unittest.main()
