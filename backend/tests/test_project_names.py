"""POST /mlflow/projects: a user's project names are unique (the CLI looks projects up by name)."""
from tests.access_api_case import AccessApiCase
from tests.scheduler_pg import requires_postgres


@requires_postgres
class ProjectNameTests(AccessApiCase):
    def _user(self, label):
        return self.new_user(label)

    def _create(self, auth, name, description=None):
        return self.client.post("/mlflow/projects", headers=auth, json={"name": name, "description": description})

    def test_create_returns_a_unique_id_and_stores_the_name_for_the_user(self):
        auth = self._user("alice")
        first, second = self._create(auth, "vision"), self._create(auth, "speech", "asr work")
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertNotEqual(first.json()["project_id"], second.json()["project_id"])
        listing = {p["name"]: p for p in self.client.get("/mlflow/projects", headers=auth).json()}
        self.assertEqual(set(listing), {"vision", "speech"})
        self.assertEqual(listing["speech"]["description"], "asr work")

    def test_same_name_twice_is_a_conflict_ignoring_case_and_spaces(self):
        auth = self._user("bob")
        self.assertEqual(self._create(auth, "Vision").status_code, 201)
        for again in ("Vision", "vision", "  VISION  "):
            r = self._create(auth, again)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, f"You already have a project named '{again.strip()}'"))
        self.assertEqual(len(self.client.get("/mlflow/projects", headers=auth).json()), 1)

    def test_two_users_can_use_the_same_name(self):
        self.assertEqual(self._create(self._user("carol"), "shared-name").status_code, 201)
        self.assertEqual(self._create(self._user("dave"), "shared-name").status_code, 201)
