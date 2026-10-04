"""
The AI event toggles of a household notifier, `/api/ai/notifiers/{notifier_id}/events` (docs/ai/PHASE2.md §8, §9):
whether it sends "recipe cards ready to review", and a test of that notification.

The permission checks are exactly those of upstream's notifier routes
(`mealie/routes/households/controller_group_notifications.py`): any member of the household, with the notifier loaded
through the household-scoped repositories, so another household's notifier is a 404. The toggle lives in the fork's
`ai_event_notifier_options` side table, never in upstream's notifier options.
"""

from fastapi import APIRouter, status
from pydantic import UUID4

from mealie.routes._base import controller
from mealie.schema.household.group_events import GroupEventNotifierPrivate
from mealie.schema.recipe_ingest import AINotifierEventsOut, AINotifierEventsUpdate
from mealie.services.ai.ingest import events
from mealie.services.ai.ingest.i18n import with_fallback

from ._deps import IngestController, ingest_error

router = APIRouter(prefix="/ai/notifiers", tags=["AI: Recipe Cards"])

NOT_FOUND = "not_found"


@controller(router)
class AINotifierEventsController(IngestController):
    def _notifier(self, notifier_id: UUID4) -> GroupEventNotifierPrivate:
        """The household's notifier, else 404"""
        notifier = self.repos.group_event_notifier.get_one(notifier_id, override_schema=GroupEventNotifierPrivate)
        if notifier is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return notifier

    @router.get("/{notifier_id}/events", response_model=AINotifierEventsOut)
    def get_notifier_events(self, notifier_id: UUID4) -> AINotifierEventsOut:
        """Which AI events the notifier sends"""
        self._notifier(notifier_id)
        options = self.ingest_repos.notifier_options.get(notifier_id)
        if options is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return options

    @router.put("/{notifier_id}/events", response_model=AINotifierEventsOut)
    def update_notifier_events(self, notifier_id: UUID4, data: AINotifierEventsUpdate) -> AINotifierEventsOut:
        self._notifier(notifier_id)
        options = self.ingest_repos.notifier_options.set(
            notifier_id, recipe_ingestion_ready=data.recipe_ingestion_ready
        )
        if options is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return options

    @router.post("/{notifier_id}/events/test", status_code=status.HTTP_204_NO_CONTENT)
    def test_notifier_events(self, notifier_id: UUID4) -> None:
        """
        Sends a test "recipe cards ready" notification through this notifier, whether or not it's switched on for
        it: the same event and data shape, with the household's current counts
        """
        notifier = self._notifier(notifier_id)
        translator = with_fallback(self.translator)
        events.send_test_notification(self.session, self.group_id, self.household_id, notifier.apprise_url, translator)
