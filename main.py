"""
Performs the full data pipeline:
    1. Fetch reviews from the Steam API
    2. Clean and deduplicate them
    3. Store in SQLite
    4. Compute and display basic statistics

Usage:
    python main.py
"""

import logging
import sys
from pipeline.ingest_reviews import fetch_all_reviews
from pipeline.clean import clean_pipeline
from pipeline.storage import get_connection, create_tables, save_reviews, load_reviews, load_classified_reviews
from pipeline.stats import compute_basic_stats, compute_keyword_frequency, print_stats_report
from pipeline.classify import run_classification
from pipeline.cluster import build_clusters, rank_clusters, summarize_cluster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def run_pipeline(app_id: str, max_reviews: int = 500, skip_fetch: bool = False) -> None:
    """
    Run the full data pipeline.

    Parameters:
        app_id: The Steam game ID to fetch reviews for.
        max_reviews: How many reviews to fetch from Steam.
        skip_fetch: If True, skip the API call and load from the database.
                    Useful when iterating on downstream steps without
                    re-fetching data each time.
    """
    # Step 0: Set up the database
    conn = get_connection()
    create_tables(conn)

    if not skip_fetch:
        # Step 1: Fetch reviews from Steam
        logger.info(f"=== STEP 1: Fetching reviews for app {app_id} ===")
        try:
            raw_reviews = fetch_all_reviews(app_id=app_id, max_reviews=max_reviews)
        except Exception as e:
            logger.error(f"Failed to fetch reviews: {e}")
            logger.info("You can re-run with skip_fetch=True if you have data in the DB.")
            conn.close()
            return

        if not raw_reviews:
            logger.error("No reviews fetched. Check the app ID and your internet connection.")
            conn.close()
            return

        # Step 2: Clean and deduplicate
        logger.info("=== STEP 2: Cleaning and deduplicating ===")
        df = clean_pipeline(raw_reviews, app_id)

        # Step 3: Store in database
        logger.info("=== STEP 3: Saving to database ===")
        save_reviews(conn, df)

        # Step 3.5: Classify reviews
        logger.info("=== STEP 3.5: Classifying reviews ===")
        run_classification(conn, app_id, limit=50)
        
        # Step 3.7: Cluster detection & priority ranking
        logger.info("=== STEP 3.7: Clustering and ranking ===")
        classified_df = load_classified_reviews(conn, app_id)

        if len(classified_df) == 0:
            logger.warning("No classified reviews found. Skipping clustering.")
        else:
            clusters = build_clusters(classified_df)
            clusters = rank_clusters(clusters)

            for cluster in clusters:
                cluster.summary = summarize_cluster(cluster)
                print(f"\n--- {cluster.category} (priority: {cluster.priority_score}) ---")
                print(cluster.summary)

    else:
        logger.info("Skipping fetch — loading from database.")
        logger.info("Classifying any unclassified reviews")
        run_classification(conn, app_id, limit=50)
        # Step 3.7: Cluster detection & priority ranking
        logger.info("=== STEP 3.7: Clustering and ranking ===")
        classified_df = load_classified_reviews(conn, app_id)

        if len(classified_df) == 0:
            logger.warning("No classified reviews found. Skipping clustering.")
        else:
            clusters = build_clusters(classified_df)
            clusters = rank_clusters(clusters)

            for cluster in clusters:
                cluster.summary = summarize_cluster(cluster)
                print(f"\n--- {cluster.category} (priority: {cluster.priority_score}) ---")
                print(cluster.summary)

    # Step 4: Load from DB and compute stats
    logger.info("=== STEP 4: Computing statistics ===")
    df = load_reviews(conn, app_id=app_id, exclude_duplicates=True)

    if len(df) == 0:
        logger.warning("No reviews in database. Run without skip_fetch first.")
        conn.close()
        return

    stats = compute_basic_stats(df)
    keywords = compute_keyword_frequency(df)
    print_stats_report(stats, keywords)

    conn.close()
    logger.info("Pipeline complete.")


if __name__ == "__main__":
    # === COMMAND LINE ARGUMENTS ===
    # This lets you run:
    # python main.py 1245620              → fetch 500 reviews (default)
    # python main.py 1245620 100          → fetch 100 reviews
    # python main.py 1245620 --skip-fetch → load from DB, no API call

    skip_fetch = "--skip-fetch" in sys.argv
    max_reviews = 500

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if len(args) < 1:
        print("Usage: python main.py [game app ID] [maximum number of reviews to fetch]")
        print("       python main.py [game app ID] --skip-fetch")
        print("\nExample: python main.py 1245620 100")
        sys.exit(1)

    app_id = args[0] if args else ""

    if len(args) > 1 and args[1].isdigit():
        max_reviews = int(args[1])

    run_pipeline(app_id=app_id, max_reviews=max_reviews, skip_fetch=skip_fetch)