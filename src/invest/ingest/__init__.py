"""Conectores de ingesta. Cada fuente implementa la interfaz de ``base.Conector``."""

from invest.ingest.banrep import ConectorBanRep
from invest.ingest.bvc import ConectorBVC, ConectorBVCIndices
from invest.ingest.cuif import ConectorCUIF
from invest.ingest.dane_ipc import ConectorDANEIPC
from invest.ingest.estados_compania import ConectorEstadosCompania
from invest.ingest.fred import ConectorFRED
from invest.ingest.sec_edgar import ConectorEDGAR
from invest.ingest.sfc import ConectorSFCEspecies, ConectorSFCFicha, ConectorSFCPrecios
from invest.ingest.sfc_niif import ConectorNIIF
from invest.ingest.sfc_relevante import ConectorSFCRelevante
from invest.ingest.supersociedades import ConectorSupersociedades
from invest.ingest.trm import ConectorTRM

CONECTORES = {
    c.fuente_id: c
    for c in (ConectorTRM, ConectorSFCEspecies, ConectorSFCPrecios, ConectorSFCFicha, ConectorSFCRelevante,
              ConectorDANEIPC, ConectorFRED, ConectorCUIF, ConectorEDGAR, ConectorBVC, ConectorBVCIndices, ConectorBanRep,
              ConectorNIIF, ConectorSupersociedades, ConectorEstadosCompania)
}
