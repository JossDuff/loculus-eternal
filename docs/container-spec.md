# Container specification

**Status: the normative body of this document is written in the container format phase.**
Until then this file holds only the pinned Pathoplexus schema below, which the format must
carry. Once the first byte is published on mainnet the format can be extended but never
changed.

## Normative stream format

*To be written in the container format phase.* The agreed shape is summarised in
`loculus-eternal-PLAN.md`: length-prefixed self-delimiting records; batches that start at a
blob boundary; record types `HEADER`, `BATCH_BEGIN`, `ENTRY`, `SCHEMA`, `TOOLING`,
`DICTIONARY` (reserved), `REPROCESSED` (reserved), `BATCH_MANIFEST`, `INDEX`; codec IDs
`0 = raw`, `1 = zstd`; canonical JSON (RFC 8785) for entries; deterministic per-organism
NDJSON materialisation sorted by accession then numeric version; an index emitted by size
threshold.

## Appendix: the Pathoplexus published-field schema

Pinned from the live backend `https://backend.pathoplexus.org` on 2026-09-30 by fetching the
first released line of every organism. The upload command checks the live schema against
this appendix and refuses to publish if a top-level key is missing or an unknown one appears,
so drift is caught before it reaches the stream. `SCHEMA` records in the stream carry the same
information for each organism so a future decoder does not depend on this file.

### Source endpoint

`GET {backend}/{organism}/get-released-data`

| Fact | Value observed |
|---|---|
| Content type | `application/x-ndjson`, one JSON object per line |
| Compression | optional query `compression=zstd`; response then has `content-encoding: zstd` |
| Caching | `ETag` header of the form `"<data timestamp>|<date>"`; `If-None-Match` returns 304 when unchanged |
| Record count | `X-Total-Records` header |
| Selection | every accessionVersion with status `APPROVED_FOR_RELEASE`, all versions, including revocations |

### Organisms (configuration, not code)


| Organism | Nucleotide segments | Amino-acid genes (count) |
|---|---|---|
| `andv` | `L`, `M`, `S` | 6 |
| `cchf` | `L`, `M`, `S` | 3 |
| `dengue` | `DENV-1`, `DENV-2`, `DENV-3`, `DENV-4` | 44 |
| `ebola-bdbv` | `main` | 8 |
| `ebola-sudan` | `main` | 9 |
| `ebola-zaire` | `main` | 9 |
| `hmpv` | `main` | 9 |
| `marburg` | `main` | 7 |
| `measles` | `main` | 8 |
| `mpox` | `main` | 175 |
| `rsv-a` | `main` | 11 |
| `rsv-b` | `main` | 11 |
| `west-nile` | `main` | 11 |
| `yellow-fever` | `main` | 12 |
| `zika` | `main` | 14 |

### Top-level keys of every line

Exactly these six, in this order as served:

| Key | Shape | Origin |
|---|---|---|
| `metadata` | object, one value per metadata field (string, number, boolean, or null) | submitted fields plus backend-added and pipeline-added fields |
| `unalignedNucleotideSequences` | object keyed by segment → string or null | submitted |
| `alignedNucleotideSequences` | object keyed by segment → string or null | preprocessing pipeline |
| `nucleotideInsertions` | object keyed by segment → array of strings | preprocessing pipeline |
| `alignedAminoAcidSequences` | object keyed by gene → string or null | preprocessing pipeline |
| `aminoAcidInsertions` | object keyed by gene → array of strings | preprocessing pipeline |

### Published record

An `ENTRY` record is the served line with one key added and four metadata fields removed:

- added: `organism` (the organism identifier from the table above), at the top level;
- removed from `metadata`: `versionStatus`, `dataUseTerms`, `dataUseTermsRestrictedUntil`, `dataUseTermsUrl`.

Reasons: `versionStatus` describes the entry relative to later versions and changes when a
revision or revocation is published, so it is recomputed from the record set at recovery
time; `dataUseTerms`, `dataUseTermsRestrictedUntil` and `dataUseTermsUrl` are always the
open-data values for anything eligible for publication and are derived from instance
configuration rather than stored per entry. Everything else, including every pipeline-added
field and `pipelineVersion`, is published exactly as served at first publication.

### Backend-added and identity fields

Present in every organism. Types are those observed in the sample lines.

| Field | Observed value or type | Notes |
|---|---|---|
| `accession` | `PP_0052WW2` | stable identifier, `PP_` prefix |
| `version` | `1` | integer, starts at 1 |
| `accessionVersion` | `PP_0052WW2.1` | `<accession>.<version>`, the record key |
| `isRevocation` | boolean | boolean |
| `versionStatus` | `LATEST_VERSION` | removed from the published record |
| `dataUseTerms` | `OPEN` | removed; `OPEN` for everything published |
| `dataUseTermsRestrictedUntil` | null | removed; null for everything published |
| `dataUseTermsUrl` | `https://pathoplexus.org/about/terms-of-use/open-data` | removed; constant per instance |
| `dataBecameOpenAt` | string | kept; when the terms became open |
| `releasedDate` | string | date |
| `releasedAtTimestamp` | integer | epoch seconds |
| `submittedDate` | string | date |
| `submittedAtTimestamp` | integer | epoch seconds |
| `submissionId` | string | submitter's own identifier |
| `submitter` | string | username |
| `groupId` | integer | integer |
| `groupName` | string | string |
| `versionComment` | null | string or null |
| `pipelineVersion` | `33` | integer; which preprocessing pipeline produced the processed fields |
| `displayName` | string | string, backend-derived |
| `earliestReleaseDate` | string | date, backend-derived |

### Metadata fields common to all organisms (115)

```
accession
accessionVersion
ampliconPcrPrimerScheme
ampliconSize
anatomicalMaterial
anatomicalPart
assemblyReferenceGenomeAccession
authorAffiliations
authors
bioprojectAccession
biosampleAccession
bodyProduct
breadthOfCoverage
cellLine
collectionDevice
collectionMethod
comment
consensusSequenceSoftwareName
consensusSequenceSoftwareVersion
cultureId
dataBecameOpenAt
dataUseTerms
dataUseTermsRestrictedUntil
dataUseTermsUrl
dehostingMethod
depthOfCoverage
diagnosticMeasurementMethod
diagnosticMeasurementUnit
diagnosticMeasurementValue
diagnosticTargetGeneName
diagnosticTargetPresence
displayName
earliestReleaseDate
environmentalMaterial
environmentalSite
experimentalSpecimenRoleType
exposureDetails
exposureEvent
exposureSetting
foodProduct
foodProductProperties
gcaAccession
geoLocAdmin1
geoLocAdmin2
geoLocCity
geoLocCountry
geoLocLatitude
geoLocLongitude
geoLocSite
groupId
groupName
hostAge
hostAgeBin
hostDisease
hostGender
hostHealthOutcome
hostHealthState
hostNameCommon
hostNameScientific
hostOriginCountry
hostRole
hostTaxonId
hostVaccinationStatus
insdcRawReadsAccession
isLabHost
isRevocation
ncbiReleaseDate
ncbiSourceDb
ncbiSubmitterCountry
ncbiVirusName
ncbiVirusTaxId
pairedEndInsertSize
passageMethod
passageNumber
pipelineVersion
presamplingActivity
previousInfectionDisease
previousInfectionOrganism
purposeOfSampling
purposeOfSequencing
qualityControlDetails
qualityControlDetermination
qualityControlIssues
qualityControlMethodName
qualityControlMethodVersion
rawReads
rawSequenceDataProcessingMethod
releasedAtTimestamp
releasedDate
sampleCollectionDate
sampleCollectionDateRangeLower
sampleCollectionDateRangeUpper
sampleReceivedDate
sampleType
sequencedByContactEmail
sequencedByContactName
sequencedByOrganization
sequencingAssayType
sequencingDate
sequencingInstrument
sequencingLibrarySelection
sequencingLibrarySource
sequencingProtocol
signsAndSymptoms
specimenCollectorSampleId
specimenProcessing
specimenProcessingDetails
submissionId
submittedAtTimestamp
submittedDate
submitter
travelHistory
version
versionComment
versionStatus
```

### Metadata fields specific to one or more organisms

Pipeline-derived per-segment fields carry a `_<segment>` suffix in segmented organisms.

- `andv` (160 fields): `completeness_L`, `completeness_M`, `completeness_S`, `frameShifts_L`, `frameShifts_M`, `frameShifts_S`, `insdcAccessionBase_L`, `insdcAccessionBase_M`, `insdcAccessionBase_S`, `insdcAccessionFull_L`, `insdcAccessionFull_M`, `insdcAccessionFull_S`, `insdcVersion_L`, `insdcVersion_M`, `insdcVersion_S`, `length_L`, `length_M`, `length_S`, `ncbiUpdateDate_L`, `ncbiUpdateDate_M`, `ncbiUpdateDate_S`, `stopCodons_L`, `stopCodons_M`, `stopCodons_S`, `totalAmbiguousNucs_L`, `totalAmbiguousNucs_M`, `totalAmbiguousNucs_S`, `totalDeletedNucs_L`, `totalDeletedNucs_M`, `totalDeletedNucs_S`, `totalFrameShifts_L`, `totalFrameShifts_M`, `totalFrameShifts_S`, `totalInsertedNucs_L`, `totalInsertedNucs_M`, `totalInsertedNucs_S`, `totalSnps_L`, `totalSnps_M`, `totalSnps_S`, `totalStopCodons_L`, `totalStopCodons_M`, `totalStopCodons_S`, `totalUnknownNucs_L`, `totalUnknownNucs_M`, `totalUnknownNucs_S`
- `cchf` (161 fields): `completeness_L`, `completeness_M`, `completeness_S`, `frameShifts_L`, `frameShifts_M`, `frameShifts_S`, `insdcAccessionBase_L`, `insdcAccessionBase_M`, `insdcAccessionBase_S`, `insdcAccessionFull_L`, `insdcAccessionFull_M`, `insdcAccessionFull_S`, `insdcVersion_L`, `insdcVersion_M`, `insdcVersion_S`, `length_L`, `length_M`, `length_S`, `lineage_S`, `ncbiUpdateDate_L`, `ncbiUpdateDate_M`, `ncbiUpdateDate_S`, `stopCodons_L`, `stopCodons_M`, `stopCodons_S`, `totalAmbiguousNucs_L`, `totalAmbiguousNucs_M`, `totalAmbiguousNucs_S`, `totalDeletedNucs_L`, `totalDeletedNucs_M`, `totalDeletedNucs_S`, `totalFrameShifts_L`, `totalFrameShifts_M`, `totalFrameShifts_S`, `totalInsertedNucs_L`, `totalInsertedNucs_M`, `totalInsertedNucs_S`, `totalSnps_L`, `totalSnps_M`, `totalSnps_S`, `totalStopCodons_L`, `totalStopCodons_M`, `totalStopCodons_S`, `totalUnknownNucs_L`, `totalUnknownNucs_M`, `totalUnknownNucs_S`
- `dengue` (133 fields): `completeness`, `frameShifts`, `gisaidIsolateId`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `lineage`, `ncbiUpdateDate`, `serotype`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `ebola-bdbv` (132 fields): `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `mutationsFromOutbreakFounder`, `ncbiUpdateDate`, `outbreak`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `ebola-sudan` (130 fields): `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `ebola-zaire` (131 fields): `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `ncbiUpdateDate`, `outbreak`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `hmpv` (131 fields): `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `lineage`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `marburg` (131 fields): `clade`, `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `measles` (132 fields): `completeness`, `frameShifts`, `genotype`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `means2Id`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `mpox` (135 fields): `clade`, `completeness`, `frameShifts`, `gisaidIsolateId`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `lineage`, `ncbiUpdateDate`, `outbreak`, `outbreakLineage`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `rsv-a` (133 fields): `completeness`, `frameShifts`, `gisaidIsolateId`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `lineage`, `ncbiUpdateDate`, `stopCodons`, `subtype`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `rsv-b` (133 fields): `completeness`, `frameShifts`, `gisaidIsolateId`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `lineage`, `ncbiUpdateDate`, `stopCodons`, `subtype`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `west-nile` (131 fields): `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `lineage`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `yellow-fever` (131 fields): `clade`, `completeness`, `frameShifts`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`
- `zika` (132 fields): `clade`, `completeness`, `frameShifts`, `gisaidIsolateId`, `insdcAccessionBase`, `insdcAccessionFull`, `insdcVersion`, `length`, `ncbiUpdateDate`, `stopCodons`, `totalAmbiguousNucs`, `totalDeletedNucs`, `totalFrameShifts`, `totalInsertedNucs`, `totalSnps`, `totalStopCodons`, `totalUnknownNucs`

### Sample line sizes

One served line per organism, uncompressed, as fetched on 2026-09-30. Sizes vary widely with
genome length; mpox (about 197 kb genomes) dominates.

| Organism | Bytes |
|---|---|
| `andv` | 10,701 |
| `cchf` | 17,077 |
| `dengue` | 17,195 |
| `ebola-bdbv` | 47,315 |
| `ebola-sudan` | 26,420 |
| `ebola-zaire` | 28,034 |
| `hmpv` | 18,136 |
| `marburg` | 24,962 |
| `measles` | 21,373 |
| `mpox` | 460,371 |
| `rsv-a` | 19,888 |
| `rsv-b` | 20,473 |
| `west-nile` | 17,155 |
| `yellow-fever` | 16,876 |
| `zika` | 16,254 |
