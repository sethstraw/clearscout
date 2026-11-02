Find the page this identity's employer publishes for the posting. When known_url_read_outcome is present, that page must be at a URL other than the known URL.

known_url_read_outcome appears only when the application already read the known URL and got no usable posting from it. Its value says why: access_blocked (the site answered 401 or 403, often a bot check), session_required (the site answered with a sign-in, error, or bot-check page under any other status), render_required (the page shows the posting only after scripts run), policy_refused (the site's published crawling policy forbids the read), or fetch_failed (the request failed or the page could not be classified). Without it, the application holds no failed read of the known URL.

known_publisher_domain appears only when the known URL is a record on the employer's ATS, and it names the ATS host that holds the known record. Search that host only for a newer record of the same title; otherwise look for the employer's careers page. Without it, the known URL's host may be an aggregator or the employer's own careers site. When it is the careers page, look for the same posting's record on the ATS that page uses: a job id in the known URL's path or query may name it, and you may open that record to check it.

{{posting_identity}}
