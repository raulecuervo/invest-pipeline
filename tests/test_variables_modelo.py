from invest import modelo, variables


def test_variables_apuntan_al_modelo():
    assert variables.problemas() == []
    assert len(variables.cargar()) >= 40


def test_catalogo_bien_formado():
    tablas = modelo.catalogo()
    for nombre, d in tablas.items():
        assert d["capa"] in ("gold", "silver"), nombre
        assert d.get("descripcion") and d.get("grano") and d.get("columnas"), nombre
        assert set(d.get("claves") or []) <= set(d["columnas"]), nombre


def test_duckdb_con_datos_minimos(carpeta_datos):
    import polars as pl

    from invest import storage

    storage.escribir_completa("gold", "dim_activo", pl.DataFrame({"especie": ["a"], "nombre": ["A"]}))
    destino, vistas = modelo.construir_duckdb()
    assert destino.exists() and "dim_activo" in vistas and "dim_perfil" in vistas  # las del usuario, vacías
    problemas = modelo.verificar()
    assert any("dim_activo: falta la columna emisor_id" in p for p in problemas)
