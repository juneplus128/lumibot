
        if not isinstance(output, dict):
            raise KoreaInvestmentAPIError(
                "Current-price response does not "
                "contain a valid output."
            )

        return self._to_number(
            output.get("stck_prpr"),
            "stck_prpr",
        )

    # ---------------------------------------------------------
    # Domestic stock order-book quote
    # ---------------------------------------------------------

    def get_quote(
        self,
        asset: Asset,
        quote: Asset = None,
        exchange: str = None,
    ) -> Quote:

        """
        Retrieve domestic stock bid/ask prices and quantities.

        Endpoint:
            /uapi/domestic-stock/v1/quotations/
            inquire-asking-price-exp-ccn

        TR ID:
            FHKST01010200

        output1:
            Order-book information.

        output2:
            Expected execution / current-price information.
        """

        stock_code = self._get_stock_code(asset)

        response = self._request(
            endpoint=(
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-asking-price-exp-ccn"
            ),
            tr_id="FHKST01010200",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
            },
        )

        output1 = response.get("output1")
        output2 = response.get("output2")

        if not isinstance(output1, dict):
            raise KoreaInvestmentAPIError(
                "Quote response does not "
                "contain a valid output1."
            )

        if not isinstance(output2, dict):
            raise KoreaInvestmentAPIError(
                "Quote response does not "
                "contain a valid output2."
            )

        quote_data = {
            "price": output2.get("stck_prpr"),
            "bid": output1.get("bidp1"),
            "ask": output1.get("askp1"),
            "bid_size": output1.get("bidp_rsqn1"),
            "ask_size": output1.get("askp_rsqn1"),
            "volume": output2.get("acml_vol"),
            "change": output2.get("prdy_vrss"),
            "percent_change": output2.get("prdy_ctrt"),
        }

        return self.build_quote(
            asset=asset,
            quote_data=quote_data,
            raw_data=response,
        )

    # ---------------------------------------------------------
    # Historical prices - not implemented yet
    # ---------------------------------------------------------

    def get_historical_prices(
        self,
        asset,
        length,
        timestep="",
        timeshift=None,
        quote=None,
        exchange=None,
        include_after_hours=True,
        **kwargs,
    ) -> Bars:

        raise NotImplementedError(
            "Historical-price endpoint mapping "
            "is the next implementation step."
        )

    # ---------------------------------------------------------
    # Option chains - outside initial project scope
    # ---------------------------------------------------------

    def get_chains(
        self,
        asset: Asset,
        quote: Asset = None,
    ) -> dict:

        raise NotImplementedError(
            "Option chains are outside the initial "
            "domestic-stock data scope."
        )

