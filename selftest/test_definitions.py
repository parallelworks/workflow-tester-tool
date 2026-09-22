import json
import unittest

from probe.definitions import DefinitionError, derive_target, load_definition, location_notes
from probe.definitions import load_tests as load_definitions
from selftest.helpers import ProbeCase, definition


class DefinitionTests(ProbeCase):
    def test_valid_definition(self):
        path = self.write_test("any/layout/works.json", definition())
        test = load_definition(path)
        self.assertEqual(test.id, "activate.parallel.works/alvaro/webshell/works")
        self.assertTrue(test.launch_target.endswith("/workflows/webshell/yamls/general.yaml@canary"))
        self.assertTrue(test.launch_target.startswith("file://"))
        self.assertEqual(test.timeout_s, 60)
        self.assertEqual(test.target.resource, "pw://alvaro/gcpsmall")
        self.assertEqual(test.target.node, "controller")

    def test_defaults(self):
        data = definition()
        del data["timeout_s"]
        test = load_definition(self.write_test("t.json", data))
        self.assertEqual(test.timeout_s, 1800)
        self.assertEqual(test.warm_marker, [])

    def test_rejects_missing_and_bad_fields(self):
        cases = [
            ({"platform": None}, "'platform'"),
            ({"name": None}, "'name'"),
            ({"name": "a b"}, "'name'"),
            ({"timeout_s": "10"}, "'timeout_s'"),
            ({"timeout_s": 0}, "'timeout_s'"),
            ({"inputs": []}, "'inputs'"),
            ({"workflow": {"repo": "x", "path": "y"}}, "'workflow'"),
            ({"workflow": {"repo": "github.com/a/b@main", "path": "y", "ref": "main"}}, "@ref"),
            ({"extra": 1}, "unknown key"),
            ({"leftover_patterns": "ttyd"}, "'leftover_patterns'"),
            ({"leftover_commands": ["docker ps"]}, "'leftover_commands'"),
            ({"leftover_commands": {"bad name": "docker ps | wc -l"}}, "'leftover_commands'"),
            ({"setup": 12}, "'setup'"),
            ({"user": "a/b"}, "'user'"),
        ]
        for overrides, needle in cases:
            data = definition()
            data.update(overrides)
            path = self.write_test("bad.json", data)
            with self.assertRaises(DefinitionError, msg=str(overrides)) as ctx:
                load_definition(path)
            self.assertIn(needle, str(ctx.exception), str(overrides))

    def test_marker_normalisation(self):
        test = load_definition(self.write_test("t.json", definition(warm_marker="${HOME}/x")))
        self.assertEqual(test.warm_marker, ["${HOME}/x"])
        self.assertEqual(test.leftover_commands, {})
        self.assertIsNone(test.setup)
        test = load_definition(self.write_test("u.json", definition(
            setup=" mkdir -p $HOME/x ", leftover_commands={"docker": "docker ps -q | wc -l"})))
        self.assertEqual(test.setup, "mkdir -p $HOME/x")
        self.assertEqual(test.leftover_commands, {"docker": "docker ps -q | wc -l"})

    def test_name_comes_from_the_file_not_the_path(self):
        path = self.write_test("somewhere/else.json", definition(name="mytest"))
        test = load_definition(path)
        self.assertEqual(test.id, "activate.parallel.works/alvaro/webshell/mytest")
        self.assertEqual(test.recommended_path, "activate.parallel.works/alvaro/webshell/mytest.json")
        tests, errors = load_definitions(self.tests_dir)
        self.assertEqual(errors, [])
        notes = location_notes(tests, self.tests_dir)
        self.assertEqual(len(notes), 1)
        self.assertIn("somewhere/else.json defines activate.parallel.works/alvaro/webshell/mytest", notes[0])
        self.write_test("activate.parallel.works/alvaro/webshell/placed.json", definition())
        tests, _ = load_definitions(self.tests_dir)
        self.assertEqual(len(location_notes(tests, self.tests_dir)), 1)

    def test_missing_name_is_an_error(self):
        data = definition()
        (self.tests_dir / "noname.json").write_text(json.dumps(data))
        tests, errors = load_definitions(self.tests_dir)
        self.assertEqual(tests, [])
        self.assertIn("'name'", errors[0])

    def test_duplicate_ids_are_errors_and_dropped(self):
        self.write_test("a/dup.json", definition())
        self.write_test("b/dup.json", definition())
        self.write_test("ok.json", definition())
        tests, errors = load_definitions(self.tests_dir)
        self.assertEqual([t.name for t in tests], ["ok"])
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicate test id", errors[0])

    def test_invalid_json_reported_with_path(self):
        (self.tests_dir / "broken.json").write_text("{not json")
        tests, errors = load_definitions(self.tests_dir)
        self.assertEqual(tests, [])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("broken.json:"))

    def test_missing_tests_dir(self):
        tests, errors = load_definitions(self.root / "nope")
        self.assertEqual(tests, [])
        self.assertIn("not found", errors[0])


class TargetTests(unittest.TestCase):
    def test_string_resources(self):
        t = derive_target({"resource": "jean", "cluster": {"scheduler": False}}, "avidalto")
        self.assertEqual((t.system, t.resource, t.type, t.node), ("jean", "pw://avidalto/jean", "cluster", "controller"))
        t = derive_target({"cluster": {"resource": "pw://alvaro/gcpsmall", "scheduler": True}}, "alvaro")
        self.assertEqual((t.system, t.resource, t.node), ("gcpsmall", "pw://alvaro/gcpsmall", "compute"))
        t = derive_target({"resource": "pw://labcluster"}, "alvaro")
        self.assertEqual((t.system, t.resource, t.namespace), ("labcluster", "pw://labcluster", None))
        self.assertEqual(derive_target({"cluster": {"resource": "pw://alvaro/gcpsmall"}}, "alvaro").namespace, "alvaro")

    def test_object_resources(self):
        t = derive_target({"resource": {"name": "raider", "uri": "pw://avidalto/raider"}, "scheduler": True}, "avidalto")
        self.assertEqual((t.system, t.resource, t.type, t.node), ("raider", "pw://avidalto/raider", "cluster", "compute"))
        t = derive_target({"resource": {"name": "k3sgpu", "type": "kubernetes"}, "k8s": {"namespace": "x"}}, "alvaro")
        self.assertEqual((t.system, t.resource, t.type, t.node), ("k3sgpu", "pw://k3sgpu", "kubernetes", None))
        t = derive_target({"k8s": {"cluster": "cloudinfra", "namespace": "n"}}, "alvaro")
        self.assertEqual((t.system, t.type), ("cloudinfra", "kubernetes"))

    def test_no_resource(self):
        t = derive_target({"script": "echo"}, "alvaro")
        self.assertEqual((t.system, t.resource, t.type, t.node), (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
