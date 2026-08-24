"""Compatibility boundary between TTC jobs and the existing scheduler schema."""

from TTC_API import (
    TTCAuthError,
    TTCClient,
    TTCJobType,
    TTCNoJobAvailable,
    TTCRateLimitError,
    TTCResponseError,
)


class API_TTC:
    """Expose TTC lifecycle operations without changing the Bumx runtime path."""

    REACTION_JOB_TYPES = (
        TTCJobType.LIKE_POST_VIP_CHEO,
        TTCJobType.LIKE_POST_VIP_RE,
        TTCJobType.REACTION_VIP_CHEO,
        TTCJobType.REACTION_VIP_RE,
    )
    COMMENT_JOB_TYPES = (TTCJobType.COMMENT,)
    LEGACY_JOB_TYPES = {
        "like_facebook": REACTION_JOB_TYPES,
        "like_poster": COMMENT_JOB_TYPES,
    }

    def __init__(self, access_token, worker_id="?"):
        self._access_token = access_token
        self._client = TTCClient(worker_id=worker_id)

    def login(self):
        return self._client.login(self._access_token)

    def set_facebook_uid(self, uid):
        return self._client.set_uid(uid)

    def fetch_jobs_for_legacy_type(self, legacy_type):
        try:
            job_types = self.LEGACY_JOB_TYPES[legacy_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported legacy job type: {legacy_type!r}") from exc

        jobs = []
        no_job = []
        for job_type in job_types:
            try:
                ttc_jobs = self._client.get_jobs(job_type)
            except TTCNoJobAvailable as exc:
                no_job.append({
                    "job_type": job_type.value,
                    "countdown": exc.countdown,
                })
                continue

            for original_job in ttc_jobs:
                scheduler_job = dict(original_job)
                scheduler_job["type"] = legacy_type
                scheduler_job["_ttc_job"] = original_job
                jobs.append(scheduler_job)

        return {"jobs": jobs, "no_job": no_job}

    def submit_job(self, job):
        original_job = job.get("_ttc_job", job) if isinstance(job, dict) else job
        return self._client.submit_job(original_job)

    def get_account_info(self):
        return self._client.get_account_info()

    def close(self):
        return self._client.close()


__all__ = [
    "API_TTC",
    "TTCAuthError",
    "TTCNoJobAvailable",
    "TTCRateLimitError",
    "TTCResponseError",
]
