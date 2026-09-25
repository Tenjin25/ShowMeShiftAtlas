# Show Me Shift Atlas

Show Me Shift Atlas is an interactive election atlas for exploring how Missouri votes and how its political geography changes over time. It brings statewide election results, district boundaries, demographics, turnout, and historical comparisons into one map designed for both statewide patterns and local detail.

## What the atlas shows

The atlas can display Missouri results by county, voting precinct, congressional district, State House district, and State Senate district. Available contests span multiple election cycles and include presidential, U.S. Senate, gubernatorial, other statewide executive, congressional, and General Assembly races where data is available.

Congressional results can be viewed on both the 2022 and 2026 district lines. This makes it possible to separate changes caused by voter behavior from changes caused by district boundaries. Where a district is unchanged between maps, the atlas preserves the same underlying district result rather than introducing a second estimate.

## Ways to explore the results

The map supports several complementary views of an election:

- **Margins** shows the strength of each Democratic or Republican result.
- **Winners** emphasizes which party carried each geography.
- **Shift** compares the selected contest with the previous comparable election.
- **Flips** highlights places that changed party preference.
- **Turnout** shows where participation was highest or lowest.
- **Demographics** adds population context to the electoral map.

Search, hover details, pinned summaries, statewide totals, district profiles, and comparison panels make it possible to move between a broad statewide picture and individual communities.

## Precinct display names

`Data/precinct_friendly_names.json` supplies reviewed venue labels for three Buchanan County precincts, including verified church affiliations and a current church name. `Data/precinct_friendly_name_sources.md` records the supporting sources. These names affect map labels only; precinct codes and election-result joins remain unchanged.

## Data and methodology

Election data is assembled from official Missouri results and public election datasets, then normalized so counties, precincts, and candidates can be compared consistently across years. Census geography and demographic data provide the map boundaries and population context. Historical precinct results are crosswalked when the election geography and the displayed district map do not line up directly.

Some historical results require allocation across changed precinct or district boundaries. Those values are best understood as geographic estimates rather than certified district-level returns. The atlas records the source and method used for its precomputed district slices so direct results, overlap allocations, and modeled values can be distinguished.

## Purpose

Show Me Shift Atlas is built to make Missouri election history easier to examine: where coalitions are growing or shrinking, which areas are moving, how turnout shapes outcomes, and how redistricting changes the way those results are represented.

## CVAP data attribution

Citizen Voting Age Population (CVAP) totals use the U.S. Census Bureau's 2020-2024 American Community Survey five-year CVAP Special Tabulation. Precinct and legacy-boundary aggregates use the Redistricting Data Hub's **2024 CVAP Data Disaggregated to 2020 Census Blocks**.

- Census source: https://www.census.gov/programs-surveys/decennial-census/about/voting-rights/cvap/2020-2024-CVAP.html
- Block-level source and processing: https://redistrictingdatahub.org/

Credit: **U.S. Census Bureau; Redistricting Data Hub.**

## Legend layout (September 2026)

The map key now uses the same expandable, scrollable category-row layout as Margin Categories for Winners, Flips, Shift, and Demographics. Each row pairs a named category with its map color and a short range or interpretation. Population Change uses the same layout where that mode is available.

Shift retains its 15-step diverging spectrum and separates Democratic and Republican movement at 0.5, 1, 5, 10, 15, 20, and 25 percentage points. Movement below 0.5 points is near-white; the 25-point-and-higher category is named **Extreme**. The blue/orange colorblind palette follows the same directional bins.
