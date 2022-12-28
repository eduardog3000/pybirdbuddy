"""Bird Buddy client module"""

from __future__ import annotations
from datetime import datetime
from typing import Union

from python_graphql_client import GraphqlClient

from . import LOGGER, VERBOSE, queries
from .birds import PostcardSighting, SightingFinishStrategy
from .const import BB_URL
from .exceptions import (
    AuthenticationFailedError,
    AuthTokenExpiredError,
    NoResponseError,
    GraphqlError,
    UnexpectedResponseError,
)
from .feed import Feed, FeedNode, FeedNodeType
from .feeder import Feeder
from .media import Collection, Media

_NO_VALUE = object()
"""Sentinel value to allow None to override a default value."""


def _redact(data, redacted: bool = True):
    """Returns a redacted string if necessary."""
    return "**REDACTED**" if redacted else data


class BirdBuddy:
    """Bird Buddy api client"""

    graphql: GraphqlClient
    _email: str
    _password: str
    _access_token: Union[str, None]
    _refresh_token: Union[str, None]
    _me: Union[dict, None]
    _feeders: dict[str, Feeder]
    _collections: dict[str, Collection]
    _last_feed_date: datetime

    def __init__(self, email: str, password: str) -> None:
        self.graphql = GraphqlClient(BB_URL)
        self._email = email
        self._password = password
        self._access_token = None
        self._refresh_token = None
        self._me = None
        self._last_feed_date = None
        self._feeders = {}
        self._collections = {}

    def _save_me(self, me_data: dict):
        if not me_data:
            return False
        me_data["__last_updated"] = datetime.now()
        self._me = me_data
        # pylint: disable=invalid-name
        for f in me_data.get("feeders", []):
            if f["id"] in self._feeders:
                # Refresh Feeder data inline
                self._feeders[f["id"]].update(f)
            else:
                self._feeders[f["id"]] = Feeder(f)
        return True

    def _needs_login(self) -> bool:
        return self._refresh_token is None

    def _needs_refresh(self) -> bool:
        return self._access_token is None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _clear(self):
        self._access_token = None
        self._refresh_token = None
        self._me = None

    async def dump_schema(self) -> dict:
        """For debugging purposes: dump the entire GraphQL schema"""
        # pylint: disable=import-outside-toplevel
        from .queries.debug import DUMP_SCHEMA

        return await self._make_request(query=DUMP_SCHEMA, auth=False)

    async def _check_auth(self) -> bool:
        if self._needs_login():
            LOGGER.debug("Login required")
            return await self._login()
        if self._needs_refresh():
            LOGGER.debug("Access token needs to be refreshed")
            await self._refresh_access_token()
        return not self._needs_login()

    async def _login(self) -> bool:
        assert self._email and self._password
        variables = {
            "emailSignInInput": {
                "email": self._email,
                "password": self._password,
            }
        }
        try:
            data = await self._make_request(
                query=queries.auth.SIGN_IN,
                variables=variables,
                auth=False,
            )
        except GraphqlError as err:
            LOGGER.exception("Error logging in: %s", err)
            raise AuthenticationFailedError(err) from err

        result = data["authEmailSignIn"]
        self._access_token = result["accessToken"]
        self._refresh_token = result["refreshToken"]
        return self._save_me(result["me"])

    async def _refresh_access_token(self) -> bool:
        assert self._refresh_token
        variables = {
            "refreshTokenInput": {
                "token": self._refresh_token,
            }
        }
        try:
            data = await self._make_request(
                query=queries.auth.REFRESH_AUTH_TOKEN,
                variables=variables,
                auth=False,
            )
        except GraphqlError as exc:
            LOGGER.exception("Error refreshing access token: %s", exc)
            self._refresh_token = None
            raise AuthenticationFailedError(exc) from exc

        tokens = data["authRefreshToken"]
        self._access_token = tokens.get("accessToken")
        self._refresh_token = tokens.get("refreshToken")
        LOGGER.info("Access token refreshed")
        return not self._needs_refresh()

    async def _make_request(
        self,
        query: str,
        variables: dict = None,
        auth: bool = True,
        reauth: bool = True,
    ) -> dict:
        """Make the request, check for errors, and return the unwrapped data"""
        if auth:
            await self._check_auth()
            headers = self._headers()
        else:
            headers = {}

        should_redact = query in [queries.auth.REFRESH_AUTH_TOKEN, queries.auth.SIGN_IN]
        LOGGER.debug(
            "> GraphQL %s, vars=%s",
            query.partition("\n")[0],  # First line of query
            _redact(variables, should_redact),
        )
        response = await self.graphql.execute_async(
            query=query,
            variables=variables,
            headers=headers,
        )

        if not response or not isinstance(response, dict):
            raise NoResponseError

        errors = response.get("errors", [])
        try:
            GraphqlError.raise_errors(errors)
        except AuthTokenExpiredError:
            self._access_token = None
            if auth and reauth:
                # login and try again
                return await self._make_request(
                    query=query,
                    variables=variables,
                    auth=auth,
                    reauth=False,
                )
            raise

        result = response.get("data")
        if not isinstance(result, dict):
            raise UnexpectedResponseError(response)

        LOGGER.log(VERBOSE, "< response: %s", _redact(result, should_redact))

        return result

    async def refresh(self) -> bool:
        """Refreshes the Bird Buddy feeder data"""
        data = await self._make_request(query=queries.me.ME)
        LOGGER.debug("Feeder data refreshed successfully: %s", data)
        return self._save_me(data["me"])

    async def feed(
        self,
        first: int = 20,
        after: str = None,
        last: int = None,
        before: str = None,
    ) -> Feed:
        """Returns the Bird Buddy Feed.

        The returned dictionary contains a `"pageInfo"` key for pagination/cursor data; and an
        `"edges"` key containing a list of FeedEdge nodes, most recent items listed first.

        :param first: Return the first N items older than `after`
        :param after: The cursor of the oldest item previously seen, to allow pagination of very long feeds
        :param last: Return the last N items newer than `before`
        :param before: The cursor of the newest item previously seen, to allow pagination of very long feeds
        :param newer_than: `datetime` or `str` of the most recent feed item previously seen
        """
        variables = {
            # $first: Int,
            # $after: String,
            # $last: Int,
            # $before: String,
            "first": first,
        }

        if after:
            # $after actually looks for _older_ items
            variables["after"] = after

        if before:
            # Not implemented: birdbuddy.exceptions.GraphqlError: 501: 'Not Implemented'
            #  variables["before"] = before
            #  variables["last"] = last if last else 20
            pass

        data = await self._make_request(query=queries.me.FEED, variables=variables)
        return Feed(data["me"]["feed"])

    async def refresh_feed(self, since: datetime | str = _NO_VALUE) -> list[FeedNode]:
        """Get only fresh feed items, new since the last Feed refresh.

        The most recent edge node timestamp will be saved as the last seen feed item,
        which will become the new default value for `since`. This can be useful to,
        for example, restore a last-seen timestamp in a new instance.

        :param since: The `datetime` after which to restrict new feed items."""
        if since == _NO_VALUE:
            since = self._last_feed_date
        if isinstance(since, str):
            since = FeedNode.parse_datetime(since)
        feed = await self.feed()
        if (newest_date := feed.newest_edge.node.created_at) != self._last_feed_date:
            LOGGER.debug(
                "Updating latest seen Feed timestamp: %s -> %s",
                self._last_feed_date,
                newest_date,
            )
            self._last_feed_date = newest_date
        return feed.filter(newer_than=since)

    async def feed_nodes(self, node_type: str) -> list[FeedNode]:
        """Returns all feed items of type ``node_type``"""
        feed = await self.feed()
        return feed.filter(of_type=node_type)

    async def new_postcards(self) -> list[FeedNode]:
        """Returns all new 'Postcard' feed items.

        These Postcard node types will be converted into sightings using ``sighting_from_postcard``.
        """
        return await self.feed_nodes(FeedNodeType.NewPostcard)

    async def sighting_from_postcard(
        self,
        postcard: str | FeedNode,
    ) -> PostcardSighting:
        """Convert a 'postcard' into a 'sighting report'.

        Next step is to choose or confirm species and then finish the sighting.
        If the sighting type is ``SightingRecognized``, we can collect the sighting with
        ``finish_postcard``.
        """
        postcard_id: str
        if isinstance(postcard, str):
            postcard_id = postcard
        elif isinstance(postcard, FeedNode):
            assert postcard.node_type == FeedNodeType.NewPostcard
            postcard_id = postcard.node_id
        variables = {
            "sightingCreateFromPostcardInput": {
                "feedItemId": postcard_id,
            }
        }
        result = await self._make_request(
            query=queries.birds.POSTCARD_TO_SIGHTING,
            variables=variables,
        )
        data = result["sightingCreateFromPostcard"]
        return PostcardSighting(data).with_postcard(postcard)

    async def finish_postcard(
        self,
        feed_item_id: str,
        sighting_result: PostcardSighting,
    ) -> bool:
        """Finish collecting the postcard in your collections.

        :param feed_item_id the id from ``new_postcards``
        :param sighting_result from ``sighting_from_postcard``, should contain sightings of type
        ``SightingRecognizedBird`` or `SightingRecognizedBirdUnlocked``.
        """
        if not isinstance(sighting_result, PostcardSighting):
            # See sighting_from_postcard()["sightingCreateFromPostcard"]
            LOGGER.warning("Unexpected sighting result: %s", sighting_result)
            return False

        report = sighting_result.report
        strategy = report.finishing_strategy

        if strategy != SightingFinishStrategy.RECOGNIZED:
            # TODO: support other finish strategies
            LOGGER.warning("Requires manual selection: %s", report)
            return False

        variables = {
            "sightingReportPostcardFinishInput": {
                "feedItemId": feed_item_id,
                "defaultCoverMedia": [
                    s.cover_media for s in report.sightings if s.is_unlocked
                ],
                "notSelectedMediaIds": [],
                "reportToken": report.token,
            }
        }
        data = await self._make_request(
            query=queries.birds.FINISH_SIGHTING,
            variables=variables,
        )
        return bool(data["sightingReportPostcardFinish"]["success"])

    async def refresh_collections(self, of_type: str = "bird") -> dict[str, Collection]:
        """Returns the remote bird collections"""
        data = await self._make_request(query=queries.me.COLLECTIONS)
        collections = {
            (c := Collection(d)).collection_id: c
            for d in data["me"]["collections"]
            # __typename: CollectionBird
            if d["__typename"] == f"Collection{of_type.capitalize()}"
        }
        self._collections.update(collections)
        return self._collections

    # TODO: does it even make sense to cache this? If it's going to change
    @property
    def collections(self) -> dict[str, Collection]:
        """Returns the last seen cached Collections. See also :func:`BirdBuddy.refresh_collections()`"""
        if self._needs_login():
            LOGGER.warning(
                "BirdBuddy is not logged in. Call refresh_collections() first"
            )
            return {}
        return self._collections

    async def collection(self, collection_id: str) -> dict[str, Media]:
        """Returns the media in the specified collection.

        The keys will be the ``media_id``, and values
        """
        variables = {
            "collectionId": collection_id,
            # other inputs: first, orderBy, last, after, before
        }
        data = await self._make_request(
            query=queries.me.COLLECTIONS_MEDIA, variables=variables
        )
        # TODO: check [collection][media][pageInfo][hasNextPage]?
        return {
            (node := edge["node"]["media"])["id"]: Media(node)
            for edge in data["collection"]["media"]["edges"]
        }

    @property
    def feeders(self) -> dict[str, Feeder]:
        """The Feeder devices associated with the account."""
        if self._needs_login():
            LOGGER.warning("BirdBuddy is not logged in. Call refresh() first")
            return {}
        return self._feeders
