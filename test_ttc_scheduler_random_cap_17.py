import copy
import unittest
from unittest.mock import call, patch

import api_ttc


def _job(kind, number):
    prefix = "L" if kind == "like_facebook" else "C"
    return {
        "type": kind,
        "_ttc_job": {
            "job_id": f"{prefix}{number}",
            "object_id": f"object-{prefix}{number}",
            "source": f"source-{prefix}{number}",
        },
    }


def _jobs(kind, count):
    return [_job(kind, number) for number in range(1, count + 1)]


def _ids(jobs):
    return [job["_ttc_job"]["job_id"] for job in jobs]


def _mixed_order(*expected_ids):
    def shuffle(items):
        by_id = {job["_ttc_job"]["job_id"]: job for job in items}
        items[:] = [by_id[job_id] for job_id in expected_ids]

    return shuffle


class SchedulerRandomCapTests(unittest.TestCase):
    def _schedule(self, jobs, enabled_types, max_jobs, randint_values, shuffle=None,
                  like_range=(5, 10), comment_range=(3, 5)):
        with (
            patch.object(api_ttc.random, "randint", side_effect=randint_values) as randint,
            patch.object(api_ttc.random, "shuffle", side_effect=shuffle) as shuffle_mock,
        ):
            result = api_ttc.schedule_jobs_by_burst(
                jobs,
                frozenset(enabled_types),
                max_jobs,
                like_batch_min=like_range[0],
                like_batch_max=like_range[1],
                comment_batch_min=comment_range[0],
                comment_batch_max=comment_range[1],
            )
        return result, randint, shuffle_mock

    def test_only_like_uses_one_random_target_instead_of_filling_max(self):
        jobs = _jobs("like_facebook", 50) + _jobs("like_poster", 50)

        result, randint, shuffle = self._schedule(
            jobs, {"like_facebook"}, 30, [7], shuffle=lambda _items: None
        )

        self.assertEqual(len(result), 7)
        self.assertEqual(_ids(result), [f"L{number}" for number in range(1, 8)])
        randint.assert_called_once_with(5, 10)
        shuffle.assert_called_once()

    def test_only_comment_uses_one_random_target(self):
        jobs = _jobs("like_facebook", 50) + _jobs("like_poster", 50)

        result, randint, shuffle = self._schedule(
            jobs, {"like_poster"}, 30, [4], shuffle=lambda _items: None
        )

        self.assertEqual(len(result), 4)
        self.assertEqual(_ids(result), ["C1", "C2", "C3", "C4"])
        randint.assert_called_once_with(3, 5)
        shuffle.assert_called_once()

    def test_both_types_resolve_once_each_and_do_not_refill_to_max(self):
        jobs = _jobs("like_facebook", 50) + _jobs("like_poster", 50)

        result, randint, shuffle = self._schedule(
            jobs,
            api_ttc.SUPPORTED_JOB_TYPES,
            30,
            [5, 3],
            shuffle=lambda _items: None,
            like_range=(5, 5),
            comment_range=(3, 3),
        )

        self.assertEqual(len(result), 8)
        self.assertEqual(_ids(result), ["L1", "L2", "L3", "L4", "L5", "C1", "C2", "C3"])
        self.assertEqual(randint.call_args_list, [call(5, 5), call(3, 3)])
        shuffle.assert_called_once()

    def test_combined_candidates_are_shuffled_as_individual_jobs(self):
        jobs = _jobs("like_facebook", 5) + _jobs("like_poster", 3)
        expected = ["L1", "C1", "L2", "C2", "L3", "C3", "L4", "L5"]

        result, randint, shuffle = self._schedule(
            jobs,
            api_ttc.SUPPORTED_JOB_TYPES,
            30,
            [5, 3],
            shuffle=_mixed_order(*expected),
            like_range=(5, 5),
            comment_range=(3, 3),
        )

        self.assertEqual(_ids(result), expected)
        self.assertEqual(len(set(_ids(result))), 8)
        self.assertEqual(sum(job["type"] == "like_facebook" for job in result), 5)
        self.assertEqual(sum(job["type"] == "like_poster" for job in result), 3)
        self.assertEqual(randint.call_count, 2)
        shuffle.assert_called_once()

    def test_different_controlled_shuffles_produce_different_valid_orders(self):
        jobs = _jobs("like_facebook", 5) + _jobs("like_poster", 3)
        orders = [
            ["L1", "C1", "L2", "C2", "L3", "C3", "L4", "L5"],
            ["C3", "L5", "C2", "L4", "L3", "C1", "L2", "L1"],
        ]
        results = []

        for order in orders:
            result, randint, shuffle = self._schedule(
                jobs,
                api_ttc.SUPPORTED_JOB_TYPES,
                30,
                [5, 3],
                shuffle=_mixed_order(*order),
                like_range=(5, 5),
                comment_range=(3, 3),
            )
            results.append(_ids(result))
            self.assertCountEqual(_ids(result), ["L1", "L2", "L3", "L4", "L5", "C1", "C2", "C3"])
            self.assertEqual(randint.call_count, 2)
            shuffle.assert_called_once()

        self.assertNotEqual(results[0], results[1])

    def test_final_global_cap_clips_only_after_full_candidate_shuffle(self):
        jobs = _jobs("like_facebook", 10) + _jobs("like_poster", 10)
        shuffled_ids = ["C1", "L1", "L2", "C2", "L3", "C3", "L4", "C4", "L5", "L6", "L7"]
        candidates_seen = []

        def controlled_shuffle(items):
            candidates_seen.extend(_ids(items))
            _mixed_order(*shuffled_ids)(items)

        result, randint, shuffle = self._schedule(
            jobs,
            api_ttc.SUPPORTED_JOB_TYPES,
            8,
            [7, 4],
            shuffle=controlled_shuffle,
            like_range=(7, 7),
            comment_range=(4, 4),
        )

        self.assertEqual(candidates_seen, [f"L{number}" for number in range(1, 8)] + [f"C{number}" for number in range(1, 5)])
        self.assertEqual(len(candidates_seen), 11)
        self.assertEqual(_ids(result), shuffled_ids[:8])
        self.assertEqual(randint.call_count, 2)
        shuffle.assert_called_once()

    def test_limited_or_zero_availability_never_compensates_or_refills(self):
        cases = [
            (
                _jobs("like_facebook", 3) + _jobs("like_poster", 4),
                [8, 4],
                ["L1", "L2", "L3", "C1", "C2", "C3", "C4"],
            ),
            (
                _jobs("like_poster", 20),
                [8, 4],
                ["C1", "C2", "C3", "C4"],
            ),
        ]

        for jobs, targets, expected_ids in cases:
            with self.subTest(expected_ids=expected_ids):
                result, randint, shuffle = self._schedule(
                    jobs,
                    api_ttc.SUPPORTED_JOB_TYPES,
                    30,
                    targets,
                    shuffle=lambda _items: None,
                    like_range=(8, 8),
                    comment_range=(4, 4),
                )
                self.assertEqual(_ids(result), expected_ids)
                self.assertEqual(randint.call_count, 2)
                shuffle.assert_called_once()

    def test_max_zero_returns_without_random_draw_or_shuffle(self):
        result, randint, shuffle = self._schedule(
            _jobs("like_facebook", 5) + _jobs("like_poster", 3),
            api_ttc.SUPPORTED_JOB_TYPES,
            0,
            [],
        )

        self.assertEqual(result, [])
        randint.assert_not_called()
        shuffle.assert_not_called()

    def test_input_and_selected_job_objects_remain_unchanged(self):
        jobs = _jobs("like_facebook", 5) + _jobs("like_poster", 3)
        content_before = copy.deepcopy(jobs)
        wrapper_ids_before = [id(job) for job in jobs]
        ttc_job_ids_before = [id(job["_ttc_job"]) for job in jobs]
        expected_order = ["C1", "L1", "C2", "L2", "C3", "L3", "L4", "L5"]

        result, _randint, _shuffle = self._schedule(
            jobs,
            api_ttc.SUPPORTED_JOB_TYPES,
            30,
            [5, 3],
            shuffle=_mixed_order(*expected_order),
            like_range=(5, 5),
            comment_range=(3, 3),
        )

        self.assertEqual(jobs, content_before)
        self.assertEqual([id(job) for job in jobs], wrapper_ids_before)
        self.assertEqual([id(job["_ttc_job"]) for job in jobs], ttc_job_ids_before)
        self.assertEqual([id(job) for job in result], [id(jobs[_ids(jobs).index(job_id)]) for job_id in expected_order])


if __name__ == "__main__":
    unittest.main()
