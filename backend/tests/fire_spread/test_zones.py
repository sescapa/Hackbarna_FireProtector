from fire_spread.zones import describe_zone


def test_describe_zone_zhr2014_schema():
    props = {"ZHR": "1", "Hectares": 57885.9, "TIPUS_0": 542.57, "TIPUS_T1": 2234.48, "TIPUS_V2": 1196.55, "TIPUS_C1": 0.0}
    z = describe_zone(props)
    assert z["name"] == "ZHR 1"
    assert z["dominantFireType"].startswith("T1")
    assert list(z["designFires"]) == ["T1", "V2"]
    assert abs(sum(z["designFires"].values()) - 1.0) < 0.01
    assert z["properties"] is props


def test_describe_zone_empty():
    z = describe_zone({})
    assert z["name"] is None and z["dominantFireType"] is None and z["designFires"] == {}
