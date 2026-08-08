import requests
import json
from datetime import datetime as dt
from urllib3.exceptions import HTTPError
from dateutil import parser
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature
import time 
import base64
import os


# Connect/read timeouts for every HTTP call. Without these, requests can hang
# forever on a dead connection and stall a bot's whole loop with quotes resting.
HTTP_TIMEOUT_SECONDS = (
    float(os.environ.get("KALSHI_HTTP_CONNECT_TIMEOUT", 5)),
    float(os.environ.get("KALSHI_HTTP_READ_TIMEOUT", 20)),
)

# Retry policy for GETs only. Reads (orders/positions/markets/fills) are
# idempotent, so reissuing one is always safe — while a single transient
# connection reset surfacing to the caller can kill an entire scheduled
# trading run. POST/DELETE stay single-shot in this client: a dropped
# response leaves it unknown whether the order/cancel actually landed, and
# blind resends could double-place.
GET_RETRY_ATTEMPTS = max(1, int(os.environ.get("KALSHI_GET_RETRY_ATTEMPTS", "3")))
GET_RETRY_BACKOFF_SECONDS = (1.0, 3.0)   # sleep before 2nd, 3rd attempt
GET_RETRYABLE_STATUSES = (429, 502, 503, 504)

# Per-process min gap between API calls (self-imposed throttle, distinct from
# the account-wide token bucket). Default 100ms = 10 calls/s. Set
# KALSHI_RATE_LIMIT_MS lower for a single bot that needs more throughput once
# the account tier allows it (IMM on Advanced: 300/s write budget) — env-
# scoped so the shared crypto fleet keeps the conservative default.
RATE_LIMIT_MS = max(0.0, float(os.environ.get("KALSHI_RATE_LIMIT_MS", "100")))


class KalshiClient:
    """A simple client that allows utils to call authenticated Kalshi API endpoints."""
    def __init__(
        self,
        host: str,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        user_id: Optional[str] = None,
    ):
        """Initializes the client and logs in the specified user.
        Raises an HttpError if the user could not be authenticated.
        """

        self.host = host
        self.key_id: key_id
        self.private_key: private_key
        self.user_id = user_id
        self.last_api_call = datetime.now()
        # Opt-in HTTP keep-alive (KALSHI_HTTP_KEEPALIVE=1): reuse one pooled
        # TLS connection instead of paying a fresh ~1s handshake on EVERY call
        # (measured 2026-07-19: ~1070ms/call cold vs ~30ms pooled). Default OFF
        # so existing bots are untouched until each opts in. Session-level
        # retries are CONNECT-ONLY: a connect failure means the request never
        # reached Kalshi, so a retry cannot double-place an order. (Transient
        # failures on GETs are additionally retried in get() itself, session
        # or not; POST/DELETE errors surface to the caller unchanged.)
        self._session = None
        if os.environ.get("KALSHI_HTTP_KEEPALIVE", "0") == "1":
            try:
                from requests.adapters import HTTPAdapter
                from urllib3.util.retry import Retry
                self._session = requests.Session()
                self._session.mount("https://", HTTPAdapter(max_retries=Retry(
                    connect=2, read=0, redirect=0, status=0, other=0)))
            except Exception:
                self._session = None   # fall back to per-call connections

    def _http(self):
        return self._session if self._session is not None else requests

    """Built in rate-limiter. We STRONGLY encourage you to keep 
    some sort of rate limiting, just in case there is a bug in your 
    code. Feel free to adjust the threshold"""
    def rate_limit(self) -> None:
        # Min gap between API calls (env-tunable, see RATE_LIMIT_MS).
        threshold_ms = RATE_LIMIT_MS
        if threshold_ms <= 0:
            self.last_api_call = datetime.now()
            return
        now = datetime.now()
        threshold_in_microseconds = 1000 * threshold_ms
        threshold_in_seconds = threshold_ms / 1000
        if now - self.last_api_call < timedelta(microseconds=threshold_in_microseconds):
            time.sleep(threshold_in_seconds)
        self.last_api_call = datetime.now()

    def post(self, path: str, body: dict) -> Any:
        """POSTs to an authenticated Kalshi HTTP endpoint.
        Returns the response body. Raises an HttpError on non-2XX results.
        """
        self.rate_limit()

        response = self._http().post(
            self.host + path, data=body, headers=self.request_headers("POST", path),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        self.raise_if_bad_response(response)
        return response.json()

    def get(self, path: str, params: Dict[str, Any] = {}) -> Any:
        """GETs from an authenticated Kalshi HTTP endpoint.
        Returns the response body. Raises an HttpError on non-2XX results.
        Transient failures (connection reset/timeout, 429/5xx) are retried up
        to GET_RETRY_ATTEMPTS times — safe because GETs are idempotent."""
        last_error = None
        for attempt in range(GET_RETRY_ATTEMPTS):
            if attempt:
                time.sleep(GET_RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(GET_RETRY_BACKOFF_SECONDS) - 1)])
            self.rate_limit()
            try:
                # Headers rebuilt on every attempt: the auth signature embeds
                # a timestamp Kalshi rejects once it drifts stale.
                response = self._http().get(
                    self.host + path, headers=self.request_headers("GET", path), params=params,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                self.raise_if_bad_response(response)
                return response.json()
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as err:
                last_error = err
            except HttpError as err:
                if err.status not in GET_RETRYABLE_STATUSES:
                    raise
                last_error = err
        raise last_error

    def delete(self, path: str, params: Dict[str, Any] = {}) -> Any:
        """Posts from an authenticated Kalshi HTTP endpoint.
        Returns the response body. Raises an HttpError on non-2XX results."""
        self.rate_limit()

        response = self._http().delete(
            self.host + path, headers=self.request_headers("DELETE", path), params=params,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        self.raise_if_bad_response(response)
        return response.json()

    def request_headers(self, method: str, path: str) -> Dict[str, Any]:
        # Get the current time
        current_time = datetime.now()

        # Convert the time to a timestamp (seconds since the epoch)
        timestamp = current_time.timestamp()

        # Convert the timestamp to milliseconds
        current_time_milliseconds = int(timestamp * 1000)
        timestampt_str = str(current_time_milliseconds)

        # remove query params
        path_parts = path.split('?')
        

        msg_string = timestampt_str + method + '/trade-api/v2' + path_parts[0]
        signature = self.sign_pss_text(msg_string)

        headers = {"Content-Type": "application/json"}

        headers["KALSHI-ACCESS-KEY"] = self.key_id
        headers["KALSHI-ACCESS-SIGNATURE"] = signature
        headers["KALSHI-ACCESS-TIMESTAMP"] = timestampt_str
        return headers
    
    def sign_pss_text(self, text: str) -> str:
        # Before signing, we need to hash our message.
        # The hash is what we actually sign.
        # Convert the text to bytes
        message = text.encode('utf-8')
        try:
            signature = self.private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
            return base64.b64encode(signature).decode('utf-8')
        except InvalidSignature as e:
            raise ValueError("RSA sign PSS failed") from e

    def raise_if_bad_response(self, response: requests.Response) -> None:
        if response.status_code not in range(200, 299):
            err = HttpError(response.reason, response.status_code)
            try:
                err.body = response.text[:500]
            except Exception:
                err.body = ""
            raise err
    
    def query_generation(self, params:dict) -> str:
        # 'self' sneaks in when callers pass locals(); it must not become a
        # query param (harmless to Kalshi, but garbage in the URL).
        relevant_params = {k:v for k,v in params.items() if v != None and k != 'self'}
        if len(relevant_params):
            query = '?'+''.join("&"+str(k)+"="+str(v) for k,v in relevant_params.items())[1:]
        else:
            query = ''
        return query

class HttpError(Exception):
    """Represents an HTTP error with reason and status code."""
    def __init__(self, reason: str, status: int):
        super().__init__(reason)
        self.reason = reason
        self.status = status

    def __str__(self) -> str:
        return "HttpError(%d %s)" % (self.status, self.reason)

class ExchangeClient(KalshiClient):
    def __init__(self, 
                    exchange_api_base: str,
                    key_id: str, 
                    private_key: rsa.RSAPrivateKey):
        super().__init__(
            exchange_api_base,
            key_id,
            private_key,
        )
        self.key_id = key_id
        self.private_key = private_key
        self.exchange_url = "/exchange"
        self.markets_url = "/markets"
        self.events_url = "/events"
        self.series_url = "/series"
        self.portfolio_url = "/portfolio"
        self.events_orders_url = self.portfolio_url + "/events/orders"

    def logout(self,):
        result = self.post("/logout")
        return result

    def get_exchange_status(self,):
        result = self.get(self.exchange_url + "/status")
        return result

    # market endpoints!

    def get_markets(self,
                        limit:Optional[int]=None,
                        cursor:Optional[str]=None,
                        event_ticker:Optional[str]=None,
                        series_ticker:Optional[str]=None,
                        max_close_ts:Optional[int]=None,
                        min_close_ts:Optional[int]=None,
                        status:Optional[str]=None,
                        tickers:Optional[str]=None,
                            ):
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        dictr = self.get(self.markets_url+query_string)
        return dictr

    def get_market_url(self, 
                        ticker:str):
        return self.markets_url+'/'+ticker

    def get_market(self, 
                    ticker:str):
        market_url = self.get_market_url(ticker=ticker)
        dictr = self.get(market_url)
        return dictr

    def get_event(self,
                    event_ticker:str):
        dictr = self.get(self.events_url+'/'+event_ticker)
        return dictr

    def get_events(self,
                    limit:Optional[int]=None,
                    cursor:Optional[str]=None,
                    status:Optional[str]=None,
                    series_ticker:Optional[str]=None,
                    ):
        # Returns a series' events newest-first. Used by the weekly one-touch
        # bot, whose event tickers are DISCOVERED rather than computed, to
        # sweep orphaned orders on recently-rolled events.
        # (with_nested_markets is deliberately omitted: it is a lowercase-bool
        # query param and query_generation would stringify Python's True.)
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        dictr = self.get(self.events_url+query_string)
        return dictr

    def get_series(self, 
                    series_ticker:str):
        dictr = self.get(self.series_url+'/'+series_ticker)
        return dictr

    def get_market_history(self, 
                            ticker:str,
                            limit:Optional[int]=None,
                            cursor:Optional[str]=None,
                            max_ts:Optional[int]=None,
                            min_ts:Optional[int]=None,
                            ):
        relevant_params = {k: v for k,v in locals().items() if k!= 'ticker'}                            
        query_string = self.query_generation(params = relevant_params)
        market_url = self.get_market_url(ticker = ticker)
        dictr = self.get(market_url + '/history' + query_string)
        return dictr

    def get_orderbook(self, 
                        ticker:str,
                        depth:Optional[int]=None,
                        ):
        relevant_params = {k: v for k,v in locals().items() if k!= 'ticker'}                            
        query_string = self.query_generation(params = relevant_params)
        market_url = self.get_market_url(ticker = ticker)
        dictr = self.get(market_url + "/orderbook" + query_string)
        return dictr

    def get_trades(self,
                    ticker:Optional[str]=None,
                    limit:Optional[int]=None,
                    cursor:Optional[str]=None,
                    max_ts:Optional[int]=None,
                    min_ts:Optional[int]=None,
                    ):
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        if ticker != None:
            if len(query_string):
                query_string += '&'
            else:
                query_string += '?'
            query_string += "ticker="+str(ticker)
            
        trades_url = self.markets_url + '/trades'
        dictr = self.get(trades_url + query_string)
        return dictr

    # portfolio endpoints!

    def get_balance(self,):
        dictr = self.get(self.portfolio_url+'/balance')
        return dictr

    # ==================================================================
    # V2 order endpoints (migrated 2026-06). Kalshi retired the legacy
    # POST/DELETE /portfolio/orders* mutation endpoints (now HTTP 410 Gone)
    # in favor of /portfolio/events/orders*. See docs.kalshi.com/changelog
    # (2026-04-22 new V2 endpoints; 2026-06-18 legacy deprecation).
    #
    # These methods keep the LEGACY call signatures (side=yes/no,
    # action=buy/sell, yes_price/no_price in integer cents, integer count,
    # type=limit/market, post_only, expiration_ts) so the trading bots'
    # call sites are unchanged. Legacy params are translated here to the V2
    # single-YES-book shape: side=bid/ask, one dollar-string `price` quoted
    # on the YES leg, string `count`, and the now-required `time_in_force`
    # and `self_trade_prevention_type`.
    #
    # YES-book mapping (Kalshi: bid=buy YES, ask=sell YES; selling YES is
    # economically buying NO at 1-price):
    #     buy  YES -> side=bid, price = yes_price
    #     sell YES -> side=ask, price = yes_price
    #     buy  NO  -> side=ask, price = 100 - no_price   (cents)
    #     sell NO  -> side=bid, price = 100 - no_price   (cents)
    # ==================================================================

    @staticmethod
    def _cents_to_dollar_str(cents) -> str:
        """Integer cents -> exact dollar string: 45 -> '0.45', 5 -> '0.05',
        99 -> '0.99'. Integer math (no float) so prices land on the penny tick."""
        c = int(round(float(cents)))
        sign = '-' if c < 0 else ''
        c = abs(c)
        return f"{sign}{c // 100}.{c % 100:02d}"

    def _build_v2_order_body(self,
                             ticker, side, action='buy', count=None,
                             type='limit', yes_price=None, no_price=None,
                             post_only=None, expiration_ts=None,
                             client_order_id=None, time_in_force=None,
                             self_trade_prevention_type=None,
                             **_ignored):
        """Translate legacy create_order kwargs into a V2 events/orders body.
        `_ignored` absorbs legacy-only params (buy_max_cost, sell_position_floor)
        that V2 does not support and the bots no longer pass."""
        s = (side or '').strip().lower()
        a = (action or 'buy').strip().lower()
        if s == 'yes':
            if yes_price is None:
                raise ValueError("create_order: yes_price required for side='yes'")
            cents = int(yes_price)
            book_side = 'bid' if a == 'buy' else 'ask'
        elif s == 'no':
            if no_price is None:
                raise ValueError("create_order: no_price required for side='no'")
            cents = 100 - int(no_price)      # buy NO == sell YES at (1 - price)
            book_side = 'ask' if a == 'buy' else 'bid'
        else:
            raise ValueError(f"create_order: unknown side {side!r} (expected 'yes'/'no')")
        if count is None:
            raise ValueError("create_order: count required")

        stp = (self_trade_prevention_type
               or os.environ.get('KALSHI_STP_TYPE', 'taker_at_cross'))

        body = {
            'ticker': ticker,
            'side': book_side,
            'count': f"{float(count):.2f}",            # V2 count is a string
            'self_trade_prevention_type': stp,
        }

        if (type or 'limit').strip().lower() == 'market':
            # V2 has no market-order type. A crossing-IOC emulation would sweep
            # the book with NO slippage cap (legacy buy_max_cost/
            # sell_position_floor have no equivalent here), risking a
            # catastrophic fill. No bot places market orders, so fail loudly
            # rather than silently ship an unprotected sweep.
            raise NotImplementedError(
                "create_order(type='market') is not supported on the V2 endpoint; "
                "use a limit price with time_in_force='immediate_or_cancel' plus an "
                "explicit slippage cap instead.")
        body['price'] = self._cents_to_dollar_str(cents)
        tif = time_in_force or 'good_till_canceled'
        body['time_in_force'] = tif
        # expiration_time (unix SECONDS) is only valid with good_till_canceled
        if expiration_ts is not None and tif == 'good_till_canceled':
            body['expiration_time'] = int(expiration_ts)

        if client_order_id is not None:
            body['client_order_id'] = client_order_id
        if post_only:
            body['post_only'] = True
        return body

    @staticmethod
    def _wrap_v2_order_response(v2):
        """Reshape the flat V2 create response (order_id, fill_count, ...) into
        the legacy {'order': {...}} envelope the bots read, while also exposing
        the flat keys at top level for any caller that reads them directly."""
        if not isinstance(v2, dict):
            return {'order': {}, 'raw': v2}
        return {'order': dict(v2), **v2}

    def create_order(self,
                        ticker:str,
                        client_order_id:str=None,
                        side:str=None,
                        action:str='buy',
                        count:int=None,
                        type:str='limit',
                        yes_price:Optional[int]=None,
                        no_price:Optional[int]=None,
                        post_only:Optional[bool]=None,
                        expiration_ts:Optional[int]=None,
                        sell_position_floor:Optional[int]=None,
                        buy_max_cost:Optional[int]=None,
                        time_in_force:Optional[str]=None,
                        self_trade_prevention_type:Optional[str]=None,
                        ):
        body = self._build_v2_order_body(
            ticker=ticker, side=side, action=action, count=count, type=type,
            yes_price=yes_price, no_price=no_price, post_only=post_only,
            expiration_ts=expiration_ts, client_order_id=client_order_id,
            time_in_force=time_in_force,
            self_trade_prevention_type=self_trade_prevention_type)
        print(f"[create_order->V2] {body}")
        order_json = json.dumps(body)
        result = self.post(path=self.events_orders_url, body=order_json)
        return self._wrap_v2_order_response(result)

    def batch_create_orders(self, orders:list):
        """V2 batch create. Each item is a legacy-style order dict (same kwargs
        as create_order); translated to the V2 shape. Unused by the bots today;
        migrated for correctness."""
        v2_orders = [self._build_v2_order_body(**o) for o in orders]
        orders_json = json.dumps({'orders': v2_orders})
        result = self.post(path=self.events_orders_url + '/batched', body=orders_json)
        return result

    def decrease_order(self, order_id:str, reduce_by:int):
        decrease_json = json.dumps({'reduce_by': reduce_by})
        result = self.post(path=self.events_orders_url + '/' + order_id + '/decrease',
                           body=decrease_json)
        return result

    def cancel_order(self, order_id:str):
        result = self.delete(path=self.events_orders_url + '/' + order_id)
        return result

    def amend_order(self, order_id:str, ticker:str, book_side:str,
                    yes_price_cents:int, total_count:float,
                    client_order_id:Optional[str]=None,
                    updated_client_order_id:Optional[str]=None):
        """V2 amend (docs: POST /portfolio/events/orders/{id}/amend, added
        for the 2026-08-02 requote-downtime fix): update a resting order's
        price and/or max-fillable count IN PLACE. The order KEEPS its
        order_id (ownership/fill matching unaffected). V2 count semantics:
        `total_count` = already-filled + desired resting remainder. Side and
        ticker cannot change; expiration_time cannot be extended; there is
        NO post_only on amend, so callers must never price through the
        opposite touch (a fast race can still take — bounded upstream by
        the join clamp). Queue position is forfeited on price changes
        (irrelevant for pro-rata reward scoring)."""
        body = {
            'ticker': ticker,
            'side': book_side,                        # 'bid' / 'ask'
            'price': self._cents_to_dollar_str(yes_price_cents),
            'count': f"{float(total_count):.2f}",
        }
        if client_order_id is not None:
            body['client_order_id'] = client_order_id
        if updated_client_order_id is not None:
            body['updated_client_order_id'] = updated_client_order_id
        print(f"[amend_order->V2] {order_id} {body}")
        result = self.post(path=self.events_orders_url + '/' + order_id + '/amend',
                           body=json.dumps(body))
        return self._wrap_v2_order_response(result)

    def batch_cancel_orders(self, order_ids:list):
        """Cancel multiple orders. The V2 batch-cancel endpoint requires a
        request body, which the base delete() cannot send; since no bot uses
        this path (they hasattr-gate on `cancel_orders`, which this client does
        not define, and fall back to per-id cancel), loop per-id cancel_order —
        always correct, no body needed."""
        return [self.cancel_order(oid) for oid in order_ids]

    # ==================================================================
    # V2 read-side normalization (migrated 2026-07-02). The same V2
    # migration wave that retired the legacy order-mutation endpoints
    # (410 Gone, see create_order above) also renamed the READ fields:
    #   positions: position -> position_fp (signed fixed-point string)
    #   orders:    count / filled_count / remaining_count ->
    #              initial_count_fp / fill_count_fp / remaining_count_fp;
    #              side/action now report the YES-book leg (a buy-NO order
    #              reads side=yes, action=sell) with the outcome side moved
    #              to `outcome_side`
    #   fills:     count -> count_fp; same YES-book side/action semantics
    # Legacy reads like p.get('position', 0) silently defaulted to zero,
    # which disabled every position-cap layer in the bots (2026-07-02 LAX
    # post-mortem: 196 contracts filled against a 150 cap).
    #
    # These helpers restore the legacy view ADDITIVELY: V2 keys are never
    # removed or renamed (several scripts read *_fp / *_dollars natively),
    # envelope keys (cursor, ...) are preserved, and synthesized counts are
    # NUMERIC floats — contract quantities are fractional post-migration.
    # One deliberate overwrite: orders'/fills' `side` and `action` are
    # rewritten to the legacy customer view ('buy' means we bought the
    # `side` outcome). Downstream P&L SQL (analysis/kxhigh/sql/
    # 90_ab_hi_no_report.sql) computes cash flow from fills.action and was
    # sign-flipped by the V2 semantics; KXHIGH_fills is truncate-rebuilt
    # nightly so it self-heals from this rewrite. The raw V2 view stays
    # available in book_side/outcome_side. KXHIGH_fills_live.action holds
    # mixed semantics across the 2026-07-02 cutover (not computed on).
    # ==================================================================

    @staticmethod
    def _fp_float(v):
        """Fixed-point string -> float ('-196.74' -> -196.74); None if absent/bad."""
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _customer_side_action(item):
        """(side, action) in legacy customer view, derived from the V2
        YES-book fields: we bought the outcome iff we were on the book side
        that acquires it (yes<->bid, no<->ask). Returns (None, None) when
        the V2 fields are missing so callers leave the item untouched."""
        outcome = item.get('outcome_side')
        book = item.get('book_side')
        if outcome in ('yes', 'no') and book in ('bid', 'ask'):
            action = 'buy' if (outcome == 'yes') == (book == 'bid') else 'sell'
            return outcome, action
        return None, None

    @classmethod
    def _normalize_position(cls, p):
        if isinstance(p, dict) and 'position' not in p:
            f = cls._fp_float(p.get('position_fp'))
            if f is not None:
                p['position'] = f
        return p

    @classmethod
    def _normalize_order(cls, o):
        if not isinstance(o, dict):
            return o
        for legacy, fp in (('count', 'initial_count_fp'),
                           ('filled_count', 'fill_count_fp'),
                           ('remaining_count', 'remaining_count_fp')):
            if legacy not in o:
                f = cls._fp_float(o.get(fp))
                if f is not None:
                    o[legacy] = f
        side, action = cls._customer_side_action(o)
        if side is not None:
            o['side'], o['action'] = side, action
        return o

    @classmethod
    def _normalize_fill(cls, f):
        if isinstance(f, dict):
            side, action = cls._customer_side_action(f)
            if side is not None:
                f['side'], f['action'] = side, action
        return f

    def get_fills(self,
                        ticker:Optional[str]=None,
                        order_id:Optional[str]=None,
                        min_ts:Optional[int]=None,
                        max_ts:Optional[int]=None,
                        limit:Optional[int]=None,
                        cursor:Optional[str]=None):

        fills_url = self.portfolio_url + '/fills'
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        dictr = self.get(fills_url + query_string)
        if isinstance(dictr, dict):
            for f in dictr.get('fills') or []:
                self._normalize_fill(f)
        return dictr

    def get_orders(self,
                        ticker:Optional[str]=None,
                        event_ticker:Optional[str]=None,
                        min_ts:Optional[int]=None,
                        max_ts:Optional[int]=None,
                        status:Optional[str]=None,
                        limit:Optional[int]=None,
                        cursor:Optional[str]=None
                        ):
        orders_url = self.portfolio_url + '/orders'
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        dictr = self.get(orders_url + query_string)
        if isinstance(dictr, dict):
            for o in dictr.get('orders') or []:
                self._normalize_order(o)
        return dictr

    def get_order(self,
                    order_id:str):
        orders_url = self.portfolio_url + '/orders'
        dictr = self.get(orders_url + '/' +  order_id)
        if isinstance(dictr, dict):
            self._normalize_order(dictr.get('order'))
        return dictr

    def get_positions(self,
                        limit:Optional[int]=None,
                        cursor:Optional[str]=None,
                        settlement_status:Optional[str]=None,
                        ticker:Optional[str]=None,
                        event_ticker:Optional[str]=None,
                        ):
        positions_url = self.portfolio_url + '/positions'
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        dictr = self.get(positions_url + query_string)
        if isinstance(dictr, dict):
            for p in dictr.get('market_positions') or []:
                self._normalize_position(p)
        return dictr

    def get_portfolio_settlements(self,
                                    limit:Optional[int]=None,
                                    cursor:Optional[str]=None,):

        positions_url = self.portfolio_url + '/settlements'
        query_string = self.query_generation(params={k: v for k,v in locals().items()})
        dictr = self.get(positions_url + query_string)
        return dictr