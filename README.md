Here is the challenge.
https://www.spaceappschallenge.org/2026/challenges/modis-and-viirs-hot-spots/

Understanding how burning activity is distributed across our planet over time, including wildfires and agricultural burns intense enough to be detected by satellite sensors, enables us to anticipate critical periods, guide monitoring efforts, and improve fire response. Satellites have tracked active fire hotspots for more than two decades, but the record is split across sensors whose data cannot be compared directly, leaving fire managers without a consistent picture of when and where burning has occurred. Your challenge is to build a web application that harmonizes these records into a burning activity calendar based on active fire hotspots, enabling early warning and emergency responders, scientists, and land managers to examine historical fire patterns, unusual conditions, and critical periods within a selected area of interest.

Our Summary:
Harmonized Burn Activity Calendar addresses a central challenge in satellite fire monitoring: understanding changes in burning activity across records collected by different instruments.

MODIS and VIIRS provide valuable observations, but differences in their sensing capabilities mean their hotspot records cannot simply be joined and treated as directly comparable. Our project aims to organise these records into a consistent burning-activity calendar, helping users explore seasonal patterns, unusual activity and critical periods within a selected region.

Our prototype provides an evidence-aware processing foundation. An ingestion engine identifies input types and quantities, distinguishing live observations, derived information and simulated data. Fusion models produce assessments with confidence scores and supporting evidence, followed by human review.

We chose this architecture because a useful historical comparison must explain both the result and the information behind it. The calendar is the intended interface for understanding activity over time, while evidence visibility helps users inspect how an assessment was produced.

The intended users include land managers, scientists and regional monitoring teams. Our next validation priority is to demonstrate MODIS–VIIRS comparability, evaluate confidence against reference cases and test whether the calendar supports meaningful interpretation. The project focuses on active-fire hotspot activity, which is distinct from total burned area.
