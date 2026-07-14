# Limitations

- The prototype is not an operational avalanche forecast.
- Catalog metadata can reveal missing or corrupted files, but it does not validate scientific suitability.
- Sampled raster min/max values are approximate for large rasters.
- Missing data is reported as missing. It is never converted to zero or treated as safe conditions.
- The known `bc_snow:2C21P:archive` HTTP 404 means historical archive coverage may be unavailable for that station.
- Avalanche Canada current products are displayed as current regional professional forecast context, not historical avalanche occurrence labels.
- Summer/off-season Avalanche Canada conditions are not interpreted as absence of avalanche risk.
- Point-cloud metadata depends on optional `laspy[lazrs]` support.
- Terrain, satellite, weather, snowpack, forecast, and susceptibility outputs require separate validation before any operational use.
