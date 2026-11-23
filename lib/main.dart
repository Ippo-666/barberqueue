import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:webview_flutter_android/webview_flutter_android.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:connectivity_plus/connectivity_plus.dart';

// =========================================================================
// 🌐 CONFIGURATION: رابط خادم المنصة (الرابط السحابي عند الرفع أو الـ IP المحلي)
// =========================================================================
class AppConfig {
  // بمجرد الرفع على Render، استبدل الرابط أدناه برابطك المباشر: https://barberqueue-dz.onrender.com
  static const String serverUrl = "https://barberqueue-dz.onrender.com/portal";
  
  // رابط احتياطي للتجربة المحلية عبر الواي فاي
  static const String localFallbackUrl = "http://192.168.100.66:5000/portal";
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // إعداد شريط الحالة وأزرار التنقل السفلية بنمط داكن فخم
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.light,
      systemNavigationBarColor: Color(0xFF0B0F19),
      systemNavigationBarIconBrightness: Brightness.light,
    ),
  );

  runApp(const BarberQueueNativeApp());
}

class BarberQueueNativeApp extends StatelessWidget {
  const BarberQueueNativeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'دورك BarberQueue DZ',
      debugShowCheckedModeBanner: false,
      locale: const Locale('ar'),
      supportedLocales: const [Locale('ar'), Locale('fr')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF0B0F19),
        primaryColor: const Color(0xFFF59E0B), // Amber 500
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFFF59E0B),
          secondary: Color(0xFF10B981), // Emerald 500
          surface: Color(0xFF111827),
        ),
        fontFamily: 'Tajawal',
      ),
      home: const SplashScreen(),
    );
  }
}

// =========================================================================
// 1. NATIVE SPLASH SCREEN (شاشة البداية الاحترافية)
// =========================================================================
class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen>
    with SingleTickerProviderStateMixin {
  late AnimationController _animController;
  late Animation<double> _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _animController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1400),
    );
    _fadeAnimation = CurvedAnimation(
      parent: _animController,
      curve: Curves.easeInOut,
    );
    _animController.forward();

    _requestPermissionsAndNavigate();
  }

  Future<void> _requestPermissionsAndNavigate() async {
    // طلب صلاحيات الـ GPS والكاميرا في الخلفية
    try {
      await [Permission.locationWhenInUse, Permission.camera].request();
    } catch (_) {}

    await Future.delayed(const Duration(milliseconds: 2200));
    if (mounted) {
      Navigator.of(context).pushReplacement(
        PageRouteBuilder(
          transitionDuration: const Duration(milliseconds: 600),
          pageBuilder: (_, __, ___) => const WebViewScreen(),
          transitionsBuilder: (_, animation, __, child) =>
              FadeTransition(opacity: animation, child: child),
        ),
      );
    }
  }

  @override
  void dispose() {
    _animController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0B0F19),
      body: Center(
        child: FadeTransition(
          opacity: _fadeAnimation,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                width: 95,
                height: 95,
                decoration: BoxDecoration(
                  gradient: const LinearGradient(
                    colors: [Color(0xFFF59E0B), Color(0xFFD97706)],
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                  ),
                  borderRadius: BorderRadius.circular(28),
                  boxShadow: [
                    BoxShadow(
                      color: const Color(0xFFF59E0B).withOpacity(0.3),
                      blurRadius: 24,
                      offset: const Offset(0, 10),
                    ),
                  ],
                ),
                child: const Center(
                  child: Text('💈', style: TextStyle(fontSize: 48)),
                ),
              ),
              const SizedBox(height: 24),
              const Text(
                'دَوْرُك',
                style: TextStyle(
                  fontSize: 32,
                  fontWeight: FontWeight.w900,
                  color: Colors.white,
                  letterSpacing: 1.2,
                ),
              ),
              const SizedBox(height: 6),
              const Text(
                'منظومة الحلاقة الذكية الأولى في الجزائر 🇩🇿',
                style: TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                  color: Color(0xFF94A3B8),
                ),
              ),
              const SizedBox(height: 36),
              const SizedBox(
                width: 24,
                height: 24,
                child: CircularProgressIndicator(
                  strokeWidth: 2.5,
                  valueColor: AlwaysStoppedAnimation<Color>(Color(0xFFF59E0B)),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// =========================================================================
// 2. MAIN WEBVIEW SCREEN (تغليف المنصة مع معالجة الروابط وفقدان الشبكة)
// =========================================================================
class WebViewScreen extends StatefulWidget {
  const WebViewScreen({super.key});

  @override
  State<WebViewScreen> createState() => _WebViewScreenState();
}

class _WebViewScreenState extends State<WebViewScreen> {
  late final WebViewController _controller;
  bool _isLoading = true;
  double _progress = 0.0;
  bool _hasError = false;
  String _currentUrl = AppConfig.serverUrl;

  @override
  void initState() {
    super.initState();
    _initWebViewController();
    _checkConnectivity();
  }

  void _initWebViewController() {
    final controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFF0B0F19))
      ..setNavigationDelegate(
        NavigationDelegate(
          onProgress: (int progress) {
            setState(() {
              _progress = progress / 100.0;
            });
          },
          onPageStarted: (String url) {
            setState(() {
              _isLoading = true;
              _hasError = false;
              _currentUrl = url;
            });
          },
          onPageFinished: (String url) {
            setState(() {
              _isLoading = false;
              _progress = 0.0;
            });
          },
          onWebResourceError: (WebResourceError error) {
            // عند انقطاع الإنترنت أو تعذر الوصول للسيرفر
            if (error.isForMainFrame ?? true) {
              setState(() {
                _hasError = true;
                _isLoading = false;
              });
            }
          },
          onNavigationRequest: (NavigationRequest request) async {
            final uri = Uri.parse(request.url);

            // 1. فتح روابط الاتصال الهاتفي مباشرة في تطبيق الهاتف الأصلي
            if (uri.scheme == 'tel') {
              if (await canLaunchUrl(uri)) {
                await launchUrl(uri);
              }
              return NavigationDecision.prevent;
            }

            // 2. فتح محادثات واتساب في تطبيق WhatsApp الأصلي
            if (uri.scheme == 'whatsapp' ||
                uri.host.contains('wa.me') ||
                uri.host.contains('whatsapp.com')) {
              if (await canLaunchUrl(uri)) {
                await launchUrl(uri, mode: LaunchMode.externalApplication);
              }
              return NavigationDecision.prevent;
            }

            // 3. فتح روابط خرائط Google Maps في تطبيق الخرائط الأصلي
            if (uri.host.contains('maps.google.com') ||
                uri.host.contains('goo.gl') ||
                uri.path.contains('/maps')) {
              if (await canLaunchUrl(uri)) {
                await launchUrl(uri, mode: LaunchMode.externalApplication);
              }
              return NavigationDecision.prevent;
            }

            // السماح بالتنقل داخل التطبيق
            return NavigationDecision.navigate;
          },
        ),
      );

    // تفعيل إعدادات متقدمة لأندرويد (تحديد الموقع داخل WebView وحفظ الجلسات)
    if (controller.platform is AndroidWebViewController) {
      AndroidWebViewController.enableDebugging(false);
      final androidController = controller.platform as AndroidWebViewController;
      androidController.setGeolocationPermissionsPromptCallbacks(
        onShowPrompt: (request) async {
          return const GeolocationPermissionsResponse(
            allow: true,
            retain: true,
          );
        },
      );
    }

    _controller = controller;
    _controller.loadRequest(Uri.parse(_currentUrl));
  }

  Future<void> _checkConnectivity() async {
    final connectivityResult = await Connectivity().checkConnectivity();
    if (connectivityResult.contains(ConnectivityResult.none)) {
      setState(() {
        _hasError = true;
      });
    }
  }

  void _reloadPage() {
    setState(() {
      _isLoading = true;
      _hasError = false;
    });
    _controller.reload();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvoked: (didPop) async {
        if (didPop) return;
        if (await _controller.canGoBack()) {
          _controller.goBack();
        } else {
          // تأكيد الخروج من التطبيق
          final shouldExit = await showDialog<bool>(
            context: context,
            builder: (ctx) => AlertDialog(
              backgroundColor: const Color(0xFF1E293B),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(20),
              ),
              title: const Text(
                'الخروج من تطبيق دورك؟',
                style: TextStyle(fontWeight: FontWeight.bold),
              ),
              content: const Text(
                'هل تريد إغلاق التطبيق ومغادرة المنصة؟',
                style: TextStyle(fontSize: 13, color: Color(0xFF94A3B8)),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(ctx).pop(false),
                  child: const Text('إلغاء', style: TextStyle(color: Colors.white)),
                ),
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFFE11D48),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                  ),
                  onPressed: () => Navigator.of(ctx).pop(true),
                  child: const Text('خروج', style: TextStyle(color: Colors.white)),
                ),
              ],
            ),
          );
          if (shouldExit ?? false) {
            SystemNavigator.pop();
          }
        }
      },
      child: Scaffold(
        backgroundColor: const Color(0xFF0B0F19),
        body: SafeArea(
          child: Stack(
            children: [
              // 1. The WebView Core
              if (!_hasError)
                RefreshIndicator(
                  color: const Color(0xFFF59E0B),
                  backgroundColor: const Color(0xFF1E293B),
                  onRefresh: () async {
                    _controller.reload();
                  },
                  child: WebViewWidget(controller: _controller),
                ),

              // 2. Custom Native Offline Error View (بديل شاشة خطأ المتصفح)
              if (_hasError)
                Center(
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 28),
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Container(
                          width: 80,
                          height: 80,
                          decoration: BoxDecoration(
                            color: const Color(0xFF1E293B),
                            borderRadius: BorderRadius.circular(24),
                            border: Border.all(
                              color: const Color(0xFFF59E0B).withOpacity(0.3),
                            ),
                          ),
                          child: const Center(
                            child: Text('📡', style: TextStyle(fontSize: 38)),
                          ),
                        ),
                        const SizedBox(height: 20),
                        const Text(
                          'تعذر الاتصال بالخادم',
                          style: TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.w900,
                            color: Colors.white,
                          ),
                        ),
                        const SizedBox(height: 8),
                        const Text(
                          'يرجى التأكد من تشغيل بيانات الهاتف (4G) أو الاتصال بشبكة Wi-Fi، ثم أعد المحاولة.',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                            fontSize: 13,
                            color: Color(0xFF94A3B8),
                            height: 1.5,
                          ),
                        ),
                        const SizedBox(height: 28),
                        ElevatedButton.icon(
                          onPressed: _reloadPage,
                          icon: const Text('🔄'),
                          label: const Text(
                            'إعادة المحاولة الآن',
                            style: TextStyle(fontWeight: FontWeight.bold),
                          ),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: const Color(0xFFF59E0B),
                            foregroundColor: const Color(0xFF0B0F19),
                            padding: const EdgeInsets.symmetric(
                              horizontal: 28,
                              vertical: 14,
                            ),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(16),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),

              // 3. Top Progress Loading Bar
              if (_isLoading && !_hasError)
                Positioned(
                  top: 0,
                  left: 0,
                  right: 0,
                  child: LinearProgressIndicator(
                    value: _progress > 0 ? _progress : null,
                    backgroundColor: Colors.transparent,
                    valueColor: const AlwaysStoppedAnimation<Color>(
                      Color(0xFFF59E0B),
                    ),
                    minHeight: 2.5,
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
