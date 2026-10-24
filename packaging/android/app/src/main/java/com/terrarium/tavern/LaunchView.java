package com.terrarium.tavern;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ColorFilter;
import android.graphics.LinearGradient;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Rect;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.animation.DecelerateInterpolator;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * Startup and failure screen over the WebView until the local page loads. It paints
 * the web canvas (web/styles/base.css body::before) so fading out reveals the same
 * light pools; colours follow web/styles/tokens.css.
 */
final class LaunchView extends FrameLayout {
    private static final int CANVAS = Color.rgb(12, 13, 15);
    private static final int TEXT_PRIMARY = Color.rgb(235, 233, 228);
    private static final int TEXT_SECONDARY = Color.rgb(182, 180, 175);
    private static final int TEXT_MUTED = Color.rgb(140, 139, 137);
    private static final int IVORY = Color.rgb(237, 234, 227);
    private static final int ON_IVORY = Color.rgb(20, 21, 23);
    private static final int ACCENT = Color.rgb(152, 172, 218);
    private static final int DANGER = Color.rgb(238, 148, 148);
    private static final int GLOW_1 = Color.rgb(92, 104, 140);

    private final float density;
    private final ProgressBar progress;
    private final TextView heading;
    private final TextView note;
    private final ScrollView technicalScroll;
    private final TextView technical;
    private final Button retry;

    LaunchView(Context context) {
        super(context);
        density = getResources().getDisplayMetrics().density;
        setBackground(new CanvasLight());
        setClickable(true); // Keeps touches off the half-loaded WebView underneath.
        setInsets(0, 0, 0, 0);

        LinearLayout column = new LinearLayout(context) {
            @Override protected void onMeasure(int width, int height) {
                // Phone-width column on tablets and landscape.
                int limit = dp(400);
                if (MeasureSpec.getSize(width) > limit) width = MeasureSpec.makeMeasureSpec(limit, MeasureSpec.EXACTLY);
                super.onMeasure(width, height);
            }
        };
        column.setOrientation(LinearLayout.VERTICAL);
        column.setGravity(Gravity.CENTER_HORIZONTAL);
        addView(column, new LayoutParams(-1, -2, Gravity.CENTER));

        // The mark sits in a soft glow-1 pool, like the web .about-mark shadow.
        GradientDrawable glow = new GradientDrawable();
        glow.setGradientType(GradientDrawable.RADIAL_GRADIENT);
        glow.setGradientCenter(0.5f, 0.55f);
        glow.setGradientRadius(dp(60));
        glow.setColors(new int[]{withAlpha(GLOW_1, 0.5f), withAlpha(GLOW_1, 0)});
        FrameLayout markBox = new FrameLayout(context);
        markBox.setBackground(glow);
        ImageView mark = new ImageView(context);
        mark.setImageResource(R.drawable.brand_mark);
        markBox.addView(mark, new LayoutParams(dp(84), dp(84), Gravity.CENTER));
        column.addView(markBox, new LinearLayout.LayoutParams(dp(168), dp(148)));

        TextView name = text(context, 26, TEXT_PRIMARY);
        name.setText("Terrarium Tavern");
        name.setTypeface(Typeface.create(Typeface.SERIF, Typeface.NORMAL));
        name.setLetterSpacing(0.02f);
        column.addView(name, margins(-2, -2, -4, 0));
        TextView tagline = text(context, 14, TEXT_MUTED);
        tagline.setText("给故事一处生长的地方。");
        column.addView(tagline, margins(-2, -2, 8, 0));

        progress = new ProgressBar(context, null, android.R.attr.progressBarStyleHorizontal);
        progress.setIndeterminate(true);
        progress.setIndeterminateTintList(ColorStateList.valueOf(ACCENT));
        column.addView(progress, margins(dp(128), -2, 44, 0));

        heading = text(context, 15, TEXT_PRIMARY);
        heading.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        column.addView(heading, margins(-2, -2, 16, 0));
        note = text(context, 13, TEXT_SECONDARY);
        note.setLineSpacing(0, 1.3f);
        column.addView(note, margins(-2, -2, 6, 0));

        // Error output can be a long traceback: it scrolls inside a well and, with
        // weight in a wrap_content column, shrinks before the retry button is pushed off.
        GradientDrawable well = new GradientDrawable();
        well.setColor(Color.rgb(16, 17, 19));
        well.setCornerRadius(dp(12));
        well.setStroke(Math.max(1, dp(1)), Color.argb(26, 255, 255, 255));
        technicalScroll = new ScrollView(context);
        technicalScroll.setBackground(well);
        technicalScroll.setPadding(dp(14), dp(12), dp(14), dp(12));
        technicalScroll.setClipToPadding(false);
        technical = text(context, 12, TEXT_SECONDARY);
        technical.setGravity(Gravity.START);
        technical.setTypeface(Typeface.MONOSPACE);
        technical.setLineSpacing(0, 1.25f);
        technical.setTextIsSelectable(true);
        technicalScroll.addView(technical);
        LinearLayout.LayoutParams technicalLayout = margins(-1, -2, 18, 0);
        technicalLayout.weight = 1;
        column.addView(technicalScroll, technicalLayout);

        GradientDrawable pill = new GradientDrawable();
        pill.setColor(IVORY);
        pill.setCornerRadius(dp(12));
        retry = new Button(context);
        retry.setText("重新启动");
        retry.setAllCaps(false);
        retry.setTextSize(15);
        retry.setTextColor(ON_IVORY);
        retry.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        retry.setBackground(new RippleDrawable(ColorStateList.valueOf(Color.argb(40, 0, 0, 0)), pill, null));
        retry.setStateListAnimator(null);
        retry.setMinWidth(dp(168));
        retry.setPadding(dp(28), 0, dp(28), 0);
        column.addView(retry, margins(-2, dp(46), 24, 0));

        column.setAlpha(0);
        column.setTranslationY(dp(10));
        column.animate().alpha(1).translationY(0).setStartDelay(60).setDuration(420)
            .setInterpolator(new DecelerateInterpolator(2));
    }

    void setOnRetry(OnClickListener listener) { retry.setOnClickListener(listener); }

    /** System bar, cutout and keyboard insets in pixels, kept outside the content. */
    void setInsets(int left, int top, int right, int bottom) {
        setPadding(dp(24) + left, dp(24) + top, dp(24) + right, dp(24) + bottom);
    }

    void showProgress(String title, String message) {
        show(title, message, null, true, TEXT_PRIMARY);
    }

    /** {@code details} is raw error output shown verbatim and selectable; null hides the well. */
    void showFailure(String title, String message, String details) {
        show(title, message, details, false, DANGER);
    }

    void hideRetry() { retry.setVisibility(GONE); }

    void dismiss() {
        animate().cancel();
        animate().alpha(0).setStartDelay(0).setDuration(240).withEndAction(() -> setVisibility(GONE));
    }

    private void show(String title, String message, String details, boolean loading, int titleColor) {
        heading.setText(title);
        heading.setTextColor(titleColor);
        note.setText(message);
        note.setVisibility(message == null ? GONE : VISIBLE);
        technical.setText(details);
        technicalScroll.setVisibility(details == null ? GONE : VISIBLE);
        technicalScroll.scrollTo(0, 0);
        progress.setVisibility(loading ? VISIBLE : GONE);
        ((LinearLayout.LayoutParams) heading.getLayoutParams()).topMargin = dp(loading ? 16 : 44);
        retry.setVisibility(loading ? GONE : VISIBLE);
        animate().cancel();
        setAlpha(1);
        setVisibility(VISIBLE);
    }

    private TextView text(Context context, float size, int color) {
        TextView view = new TextView(context);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setGravity(Gravity.CENTER_HORIZONTAL);
        return view;
    }

    private LinearLayout.LayoutParams margins(int width, int height, int topDp, int bottomDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(width, height);
        params.topMargin = dp(topDp);
        params.bottomMargin = dp(bottomDp);
        return params;
    }

    private int dp(float value) { return Math.round(value * density); }

    private static int withAlpha(int color, float alpha) {
        return Color.argb(Math.round(alpha * 255), Color.red(color), Color.green(color), Color.blue(color));
    }

    /** The web canvas: a vertical ink gradient with four elliptical light pools at the corners. */
    private static final class CanvasLight extends Drawable {
        // {centre x, centre y, radius x, radius y} as fractions of the bounds, colour, alpha.
        private static final float[][] POOLS = {
            {0f, 0f, 0.42f, 0.52f}, {1f, 0f, 0.30f, 0.40f}, {0.92f, 1.04f, 0.46f, 0.50f}, {0.06f, 1.04f, 0.36f, 0.44f},
        };
        private static final int[] POOL_COLORS = {
            Color.rgb(92, 104, 140), Color.rgb(70, 110, 112), Color.rgb(84, 88, 118), Color.rgb(150, 118, 86),
        };
        private static final float[] POOL_ALPHA = {0.34f, 0.2f, 0.26f, 0.14f};
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.DITHER_FLAG);
        private Shader base;
        private final Shader[] pools = new Shader[POOLS.length];

        @Override protected void onBoundsChange(Rect bounds) {
            float width = bounds.width();
            float height = bounds.height();
            if (width <= 0 || height <= 0) return;
            base = new LinearGradient(0, bounds.top, 0, bounds.bottom,
                new int[]{Color.rgb(27, 28, 30), CANVAS, Color.rgb(10, 11, 12)}, new float[]{0, 0.55f, 1}, Shader.TileMode.CLAMP);
            for (int index = 0; index < POOLS.length; index++) {
                float[] pool = POOLS[index];
                float radiusX = pool[2] * width;
                RadialGradient light = new RadialGradient(0, 0, radiusX,
                    new int[]{withAlpha(POOL_COLORS[index], POOL_ALPHA[index]), withAlpha(POOL_COLORS[index], 0)},
                    new float[]{0, 0.72f}, Shader.TileMode.CLAMP);
                Matrix ellipse = new Matrix();
                ellipse.setScale(1, pool[3] * height / radiusX);
                ellipse.postTranslate(bounds.left + pool[0] * width, bounds.top + pool[1] * height);
                light.setLocalMatrix(ellipse);
                pools[index] = light;
            }
        }

        @Override public void draw(Canvas canvas) {
            if (base == null) return;
            paint.setShader(base);
            canvas.drawRect(getBounds(), paint);
            for (Shader pool : pools) {
                paint.setShader(pool);
                canvas.drawRect(getBounds(), paint);
            }
        }

        @Override public void setAlpha(int alpha) { /* Fades go through the view's alpha. */ }
        @Override public void setColorFilter(ColorFilter filter) { /* Never tinted. */ }
        @Override public int getOpacity() { return PixelFormat.OPAQUE; }
    }
}
