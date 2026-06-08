"""
Polymarket CLOB client with authentication.
Handles L1 (private key) and L2 (API key) authentication.
"""
from typing import Dict, Any, List, Optional
import logging
import httpx
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds,
    OrderArgs,  # aliased to OrderArgsV2 in the V2 client
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client_v2.constants import POLYGON

from .signer import OrderSigner

logger = logging.getLogger(__name__)

# Polymarket Data API (positions/holdings are not served by the CLOB client).
DATA_API_URL = "https://data-api.polymarket.com"


class PolymarketClient:
    """
    Authenticated client for Polymarket CLOB API.

    Features:
    - L1 authentication with private key signing
    - L2 authentication with API key HMAC
    - Auto-creation of API credentials if not provided
    - Comprehensive market and trading operations
    """

    def __init__(
        self,
        private_key: str,
        address: str,
        chain_id: int = 137,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        host: str = "https://clob.polymarket.com",
        signature_type: int = 0,
        funder: Optional[str] = None,
    ):
        """
        Initialize Polymarket client.

        Args:
            private_key: Polygon wallet private key
            address: Polygon wallet address
            chain_id: Chain ID (137 for mainnet, 80002 for Amoy testnet)
            api_key: Optional L2 API key
            api_secret: Optional L2 API secret (same as passphrase)
            passphrase: Optional L2 API passphrase
            host: CLOB API host URL
            signature_type: 0=EOA, 1=email/Magic proxy, 2=browser proxy
            funder: Address holding funds (required for proxy signature types)
        """
        self.private_key = private_key
        self.address = address.lower()
        self.chain_id = chain_id
        self.host = host
        self.signature_type = signature_type
        # Address that actually holds funds; for proxy accounts this differs
        # from the signing key's address. Falls back to the signer address.
        self.funder = funder.lower() if funder else self.address

        # Initialize order signer
        self.signer = OrderSigner(private_key, chain_id)

        # L2 API credentials
        self.api_creds: Optional[ApiCreds] = None
        if api_key and (api_secret or passphrase):
            secret = api_secret or passphrase
            self.api_creds = ApiCreds(
                api_key=api_key,
                api_secret=secret,
                api_passphrase=secret
            )

        # Initialize CLOB client
        self.client: Optional[ClobClient] = None
        self._initialize_client()

        logger.info(
            f"PolymarketClient initialized for {self.address} "
            f"(chain_id: {chain_id}, L2 auth: {self.api_creds is not None})"
        )

    def _initialize_client(self) -> None:
        """Initialize the ClobClient with appropriate authentication"""
        try:
            # Build client arguments
            client_args = {
                "host": self.host,
                "chain_id": self.chain_id,
                "key": self.private_key,
            }

            # Proxy wallets (email/Magic or browser proxy) sign with the key but
            # trade on behalf of a separate funded address.
            if self.signature_type != 0:
                client_args["signature_type"] = self.signature_type
                client_args["funder"] = self.funder

            # Add L2 credentials if available
            if self.api_creds:
                client_args["creds"] = self.api_creds

            # Create client
            self.client = ClobClient(**client_args)

            logger.info("ClobClient initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize ClobClient: {e}")
            raise

    def get_client(self) -> ClobClient:
        """
        Get the underlying ClobClient instance.

        Returns:
            ClobClient instance

        Raises:
            RuntimeError: If client not initialized
        """
        if not self.client:
            raise RuntimeError("ClobClient not initialized")
        return self.client

    async def create_api_credentials(self, nonce_timeout: int = 3600) -> ApiCreds:
        """
        Create L2 API credentials for this wallet.

        This is required for authenticated operations like posting orders.
        The credentials are created once and can be reused.

        Args:
            nonce_timeout: Nonce timeout in seconds (default: 1 hour)

        Returns:
            ApiCreds object with api_key, api_secret, api_passphrase

        Raises:
            Exception: If credential creation fails
        """
        try:
            logger.info("Creating/deriving API credentials...")

            # create_or_derive: creates a new L2 key if none exists, otherwise
            # deterministically derives the existing one. Plain create_api_key()
            # returns HTTP 400 when the account already has a key.
            # (V2 client renamed this from create_or_derive_api_creds.)
            creds = self.client.create_or_derive_api_key()

            # Store credentials
            self.api_creds = ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase
            )

            # Reinitialize client with new credentials
            self._initialize_client()

            logger.info(f"API credentials created: {creds.api_key[:8]}...")
            return self.api_creds

        except Exception as e:
            logger.error(f"Failed to create API credentials: {e}")
            raise

    async def get_markets(
        self,
        next_cursor: Optional[str] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Fetch markets from Polymarket.

        Args:
            next_cursor: Pagination cursor
            limit: Number of markets to fetch (max 100)

        Returns:
            Dictionary with markets data
        """
        try:
            # Use simplified markets endpoint
            markets = self.client.get_markets(next_cursor=next_cursor)
            return markets

        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            raise

    async def get_market(self, condition_id: str) -> Dict[str, Any]:
        """
        Fetch single market by condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            Market data dictionary
        """
        try:
            market = self.client.get_market(condition_id)
            return market

        except Exception as e:
            logger.error(f"Failed to fetch market {condition_id}: {e}")
            raise

    async def get_orderbook(
        self,
        token_id: str
    ) -> Dict[str, Any]:
        """
        Fetch order book for a token.

        Args:
            token_id: Token ID to fetch orderbook for

        Returns:
            Order book with bids and asks
        """
        try:
            ob = self.client.get_order_book(token_id)

            # The CLOB returns bids/asks unsorted (and the V2 client returns a
            # plain dict while V1 returned an object). Callers expect plain
            # dicts with the best price at index 0, so normalize and sort here.
            def _field(obj, key):
                return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

            def _levels(side, best_first_desc):
                rows = [
                    {"price": _field(lvl, "price"), "size": _field(lvl, "size")}
                    for lvl in (side or [])
                ]
                rows.sort(key=lambda r: float(r["price"] or 0),
                          reverse=best_first_desc)
                return rows

            return {
                "bids": _levels(_field(ob, "bids"), True),    # highest (best) bid first
                "asks": _levels(_field(ob, "asks"), False),   # lowest (best) ask first
                "asset_id": _field(ob, "asset_id") or token_id,
                "market": _field(ob, "market"),
                "timestamp": _field(ob, "timestamp"),
            }

        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            raise

    async def get_price(
        self,
        token_id: str,
        side: str
    ) -> float:
        """
        Get current price for a token.

        Args:
            token_id: Token ID
            side: BUY or SELL

        Returns:
            Price as float
        """
        try:
            price_data = self.client.get_price(token_id, side.upper())
            return float(price_data.get("price", 0))

        except Exception as e:
            logger.error(f"Failed to fetch price for {token_id}: {e}")
            raise

    async def post_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
        expiration: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Post a limit order.

        Args:
            token_id: Token ID to trade
            price: Limit price (0-1 for probabilities)
            size: Order size in shares
            side: BUY or SELL
            order_type: Order type (GTC, FOK, GTD)
            expiration: Order expiration timestamp (required for GTD)

        Returns:
            Order response dictionary

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError(
                "L2 API credentials required for posting orders. "
                "Call create_api_credentials() first."
            )

        try:
            # Build order args. OrderArgs does NOT take order_type; the type is
            # supplied to post_order below.
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
            )

            if expiration:
                order_args.expiration = expiration

            # create_order only builds & signs the order locally; post_order
            # actually submits it to the CLOB. Both steps are required.
            signed_order = self.client.create_order(order_args)
            order_type_enum = getattr(
                OrderType, str(order_type).upper(), OrderType.GTC
            )
            order_response = self.client.post_order(signed_order, order_type_enum)

            logger.info(
                f"Order posted: {side} {size} @ {price} "
                f"(token: {token_id}, order_id: {order_response.get('orderID')})"
            )

            return order_response

        except Exception as e:
            logger.error(f"Failed to post order: {e}")
            raise

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an open order.

        Args:
            order_id: ID of order to cancel

        Returns:
            Cancellation response

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required for canceling orders")

        try:
            response = self.client.cancel(order_id)

            logger.info(f"Order cancelled: {order_id}")
            return response

        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise

    async def cancel_all_orders(self) -> Dict[str, Any]:
        """
        Cancel all open orders.

        Returns:
            Cancellation response

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            response = self.client.cancel_all()

            logger.info("All orders cancelled")
            return response

        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            raise

    async def get_orders(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get user's open orders.

        Args:
            market: Filter by market ID
            asset_id: Filter by asset ID

        Returns:
            List of open orders

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            # Build params
            params = {}
            if market:
                params["market"] = market
            if asset_id:
                params["asset_id"] = asset_id

            orders = self.client.get_orders(**params)
            return orders

        except Exception as e:
            logger.error(f"Failed to fetch orders: {e}")
            raise

    async def get_positions(self) -> List[Dict[str, Any]]:
        """
        Get user's positions from the Polymarket Data API.

        The CLOB client does not expose positions; they are served by the
        public Data API keyed on the funded (proxy) address. Each position is
        normalized to include the keys downstream code expects
        (asset_id, market, size, avg_price, current_price, unrealized_pnl)
        while preserving the original Data API fields.

        Returns:
            List of position dicts
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{DATA_API_URL}/positions",
                    params={"user": self.funder, "sizeThreshold": "0.1"},
                    timeout=20.0,
                )
                response.raise_for_status()
                raw_positions = response.json()

            positions: List[Dict[str, Any]] = []
            for p in raw_positions:
                positions.append({
                    **p,
                    "asset_id": p.get("asset", ""),
                    "market": p.get("conditionId", ""),
                    "size": float(p.get("size", 0) or 0),
                    "avg_price": float(p.get("avgPrice", 0) or 0),
                    "current_price": float(p.get("curPrice", 0) or 0),
                    "unrealized_pnl": float(p.get("cashPnl", 0) or 0),
                })
            return positions

        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            raise

    async def get_balance(self) -> Dict[str, float]:
        """
        Get user's USDC (collateral) balance via the CLOB balance-allowance
        endpoint. Balance is returned by the API in USDC base units (6
        decimals) and converted to dollars here.

        Returns:
            Dict with 'balance' (USDC, dollars), 'raw' (base units), 'address'

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.signature_type,
                )
            )
            raw = int(result.get("balance", 0) or 0)
            return {
                "balance": raw / 1_000_000,
                "raw": raw,
                "address": self.funder,
            }

        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            raise

    def has_api_credentials(self) -> bool:
        """Check if L2 API credentials are available"""
        return self.api_creds is not None

    def get_address(self) -> str:
        """Get wallet address"""
        return self.address

    def get_chain_id(self) -> int:
        """Get chain ID"""
        return self.chain_id


def create_polymarket_client(
    private_key: str,
    address: str,
    chain_id: int = 137,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    passphrase: Optional[str] = None,
    signature_type: int = 0,
    funder: Optional[str] = None,
) -> PolymarketClient:
    """
    Create PolymarketClient instance.

    Args:
        private_key: Polygon wallet private key
        address: Polygon wallet address
        chain_id: Chain ID (default: 137)
        api_key: Optional L2 API key
        api_secret: Optional L2 API secret
        passphrase: Optional L2 API passphrase
        signature_type: 0=EOA, 1=email/Magic proxy, 2=browser proxy
        funder: Address holding funds (required for proxy signature types)

    Returns:
        PolymarketClient instance
    """
    return PolymarketClient(
        private_key=private_key,
        address=address,
        chain_id=chain_id,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        signature_type=signature_type,
        funder=funder,
    )
