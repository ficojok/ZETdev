# ZETdev
Program that analyse and collect data from the Zagreb public transport company ZET.

# Needed Files and data access
For running the program u need some txt data files that need to be in the same directory as the program running. 

Files can be downloaded on https://qrac.life/zetdatagh, it includes:

-agency.txt - GTFS Static Agency Information
-calendar.txt - GTFS Static Service Date Information
-calendar_dates.txt - GTFS Static Service Date Information
-feed_info.txt - GTFS Static Agency Feed Information
-routes.txt - GTFS Static Routes Information
-shapes.txt - GTFS Static Shapes
-stop_times.txt - GTFS Static Stop Times
-stops.txt - GTFS Static Stops Information
-trips.txt - GTFS Static Trips
-voznipark.txt - internal ID, national registration and model of the BUSes

We could not put some of the files on GitHub becouse of size limitations.

The realtime GTFS data is fetched directrly from the server with every program run.
